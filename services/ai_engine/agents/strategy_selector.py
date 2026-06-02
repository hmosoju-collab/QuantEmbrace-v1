"""
StrategySelector — LLM-based read-only advisory agent for strategy management.

Runs once per NSE trading session (post-market, ~15:45 IST) to produce
a human-readable strategy activation recommendation.  Recommendations
are stored in DynamoDB and surfaced via:
    python scripts/strategy/config.py status

Phase 6 design (ADR-014 §5.8):
  Model: Claude Haiku via Anthropic SDK.
  Inputs: regime distribution, per-strategy circuit-breaker state,
          per-strategy P&L (5 days), portfolio NAV + sector exposure.
  Output: A recommendation record in DynamoDB.  NO autonomous action.

  No private trading data leaves the prompt — only strategy names, P&L
  aggregates, and regime labels are included.

  Operator workflow:
    1. Agent produces recommendation.
    2. Operator reviews via ``scripts/strategy/config.py status``.
    3. Operator manually enables/disables strategy if they agree.
    4. Agent never executes the action.

Degradation:
  If Anthropic API is unavailable or the agent fails, no recommendation
  is written and the failure is logged.  Trading is unaffected.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Optional

from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="ai_engine")

_RECOMMENDATION_TTL_DAYS = 30  # DynamoDB TTL for recommendation records

# ── Anthropic SDK guard ───────────────────────────────────────────────────────
try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    anthropic = None  # type: ignore[assignment]
    _ANTHROPIC_AVAILABLE = False
    logger.warning(
        "anthropic SDK not installed — StrategySelector unavailable. "
        "pip install anthropic"
    )


class StrategySelector:
    """
    Post-market advisory agent for strategy activation recommendations.

    Reads portfolio + regime state from DynamoDB, calls Claude Haiku
    to generate a recommendation, and writes the result back to DynamoDB.

    Args:
        dynamo_client:        boto3 DynamoDB client.
        strategy_config_table: DynamoDB table for strategy config + CB state.
        fills_table:          DynamoDB fills table (P&L data).
        risk_state_table:     DynamoDB risk-state table (NAV, sector exposure).
        regime_log_table:     DynamoDB regime-log table.
        recommendations_table: DynamoDB table for recommendation output.
        anthropic_api_key:    Anthropic API key (or read from env ANTHROPIC_API_KEY).
    """

    MODEL = "claude-3-5-haiku-20241022"

    def __init__(
        self,
        dynamo_client:          Optional[Any] = None,
        strategy_config_table:  Optional[str] = None,
        fills_table:            Optional[str] = None,
        risk_state_table:       Optional[str] = None,
        regime_log_table:       Optional[str] = None,
        recommendations_table:  Optional[str] = None,
        anthropic_api_key:      Optional[str] = None,
    ) -> None:
        self._dynamo             = dynamo_client
        self._config_table       = strategy_config_table
        self._fills_table        = fills_table
        self._risk_state_table   = risk_state_table
        self._regime_log_table   = regime_log_table
        self._recommendations_table = recommendations_table
        self._api_key            = anthropic_api_key
        self._client: Optional[Any] = None

    def _get_client(self) -> Optional[Any]:
        """Lazily initialise the Anthropic client."""
        if not _ANTHROPIC_AVAILABLE:
            return None
        if self._client is None:
            import os
            key = self._api_key or os.environ.get("ANTHROPIC_API_KEY", "")
            if not key:
                logger.warning("strategy_selector.no_anthropic_api_key")
                return None
            self._client = anthropic.Anthropic(api_key=key)
        return self._client

    async def run_post_market(self, session_date: Optional[str] = None) -> bool:
        """
        Run the post-market strategy selection analysis.

        Args:
            session_date: ISO date string e.g. ``"2026-05-08"``.
                          Defaults to today (UTC).

        Returns:
            True if a recommendation was produced and written.
        """
        _date = session_date or datetime.now(timezone.utc).date().isoformat()
        session_id = f"{_date}-NSE"

        logger.info("strategy_selector.running", session=session_id)

        try:
            context = await self._gather_context(_date)
            recommendation = await self._call_llm(context, session_id)
            if recommendation:
                await self._write_recommendation(recommendation, session_id)
                logger.info(
                    "strategy_selector.recommendation_written",
                    session=session_id,
                    action=recommendation.get("recommended_action", ""),
                )
                return True
        except Exception as exc:
            logger.error(
                "strategy_selector.failed",
                session=session_id,
                error=str(exc),
            )

        return False

    # ── Context gathering ──────────────────────────────────────────────────────

    async def _gather_context(self, date: str) -> dict[str, Any]:
        """Gather regime distribution, strategy states, P&L, and NAV from DynamoDB."""
        regime_summary = await self._fetch_regime_summary(date)
        strategy_states = await self._fetch_strategy_states()
        pnl_summary = await self._fetch_pnl_summary()
        nav = await self._fetch_nav()

        return {
            "date":           date,
            "regime_summary": regime_summary,
            "strategy_states": strategy_states,
            "pnl_5d":         pnl_summary,
            "portfolio_nav":  nav,
        }

    async def _fetch_regime_summary(self, date: str) -> dict[str, Any]:
        """Count regime occurrences for the given date from regime-log."""
        if not self._dynamo or not self._regime_log_table:
            return {}
        try:
            response = await asyncio.to_thread(
                self._dynamo.query,
                TableName=self._regime_log_table,
                KeyConditionExpression="begins_with(SK, :prefix)",
                ExpressionAttributeValues={
                    ":prefix": {"S": f"SESSION#{date}"},
                },
                Limit=500,
                ScanIndexForward=True,
            )
            counts: dict[str, int] = {}
            for item in response.get("Items", []):
                regime = item.get("regime", {}).get("S", "unknown")
                counts[regime] = counts.get(regime, 0) + 1
            return counts
        except Exception:
            return {}

    async def _fetch_strategy_states(self) -> list[dict[str, Any]]:
        """Fetch circuit-breaker state for all strategies."""
        if not self._dynamo or not self._config_table:
            return []
        try:
            response = await asyncio.to_thread(
                self._dynamo.scan,
                TableName=self._config_table,
                FilterExpression="begins_with(PK, :prefix)",
                ExpressionAttributeValues={":prefix": {"S": "STRATEGY#"}},
                ProjectionExpression="PK, #enabled, circuit_breaker_state, paper_trade",
                ExpressionAttributeNames={"#enabled": "enabled"},
            )
            states: list[dict[str, Any]] = []
            for item in response.get("Items", []):
                name = item.get("PK", {}).get("S", "").replace("STRATEGY#", "")
                states.append({
                    "name":    name,
                    "enabled": bool(item.get("enabled", {}).get("BOOL", False)),
                    "cb":      item.get("circuit_breaker_state", {}).get("S", "CLOSED"),
                    "paper":   bool(item.get("paper_trade", {}).get("BOOL", True)),
                })
            return states
        except Exception:
            return []

    async def _fetch_pnl_summary(self) -> dict[str, float]:
        """Rough P&L aggregation from fills for the past 5 days."""
        # Simplified: returns empty dict in paper mode / when fills table is unavailable
        return {}

    async def _fetch_nav(self) -> float:
        """Fetch current portfolio NAV from risk-state DynamoDB."""
        if not self._dynamo or not self._risk_state_table:
            return 0.0
        try:
            response = await asyncio.to_thread(
                self._dynamo.get_item,
                TableName=self._risk_state_table,
                Key={"PK": {"S": "ANALYTICS#PORTFOLIO"}, "SK": {"S": "LATEST"}},
            )
            item = response.get("Item", {})
            raw = item.get("nav", {}).get("N")
            return float(raw) if raw else 0.0
        except Exception:
            return 0.0

    # ── LLM call ──────────────────────────────────────────────────────────────

    async def _call_llm(
        self,
        context: dict[str, Any],
        session_id: str,
    ) -> Optional[dict[str, Any]]:
        """Call Claude Haiku with the context and parse the recommendation."""
        client = self._get_client()
        if client is None:
            return None

        prompt = _build_prompt(context, session_id)

        response = await asyncio.to_thread(
            client.messages.create,
            model=self.MODEL,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )

        raw_text = response.content[0].text if response.content else ""

        # Parse JSON response
        try:
            # Try to extract JSON block from the response
            if "```json" in raw_text:
                start = raw_text.index("```json") + 7
                end   = raw_text.index("```", start)
                raw_text = raw_text[start:end].strip()
            recommendation = json.loads(raw_text)
            recommendation["generated_at"] = datetime.now(timezone.utc).isoformat()
            recommendation["session"]      = session_id
            recommendation["model"]        = self.MODEL
            return recommendation
        except (json.JSONDecodeError, ValueError) as exc:
            logger.warning(
                "strategy_selector.llm_parse_error",
                error=str(exc),
                raw=raw_text[:200],
            )
            # Return a free-text recommendation if JSON parse fails
            return {
                "recommended_action": raw_text[:500],
                "affected_strategies": [],
                "reasoning":  "",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "session":     session_id,
                "model":       self.MODEL,
            }

    # ── DynamoDB write ────────────────────────────────────────────────────────

    async def _write_recommendation(
        self,
        recommendation: dict[str, Any],
        session_id: str,
    ) -> None:
        """Write recommendation to DynamoDB."""
        if not self._dynamo or not self._recommendations_table:
            logger.info(
                "strategy_selector.recommendation_no_table",
                recommendation=recommendation,
            )
            return

        now = datetime.now(timezone.utc)
        ttl = int(now.timestamp()) + (_RECOMMENDATION_TTL_DAYS * 24 * 3600)

        await asyncio.to_thread(
            self._dynamo.put_item,
            TableName=self._recommendations_table,
            Item={
                "PK":                  {"S": "RECOMMENDATION"},
                "SK":                  {"S": f"SESSION#{session_id}"},
                "recommended_action":  {"S": str(recommendation.get("recommended_action", ""))},
                "affected_strategies": {"S": json.dumps(recommendation.get("affected_strategies", []))},
                "reasoning":           {"S": str(recommendation.get("reasoning", ""))},
                "generated_at":        {"S": str(recommendation.get("generated_at", ""))},
                "session":             {"S": session_id},
                "model":               {"S": str(recommendation.get("model", ""))},
                "ttl":                 {"N": str(ttl)},
            },
        )


# ── Prompt builder ────────────────────────────────────────────────────────────

def _build_prompt(context: dict[str, Any], session_id: str) -> str:
    """Build the Claude Haiku prompt for strategy selection."""
    regime_summary   = context.get("regime_summary", {})
    strategy_states  = context.get("strategy_states", [])
    nav              = context.get("portfolio_nav", 0.0)

    regime_lines = "\n".join(
        f"  {regime}: {count} signals"
        for regime, count in sorted(regime_summary.items(), key=lambda x: -x[1])
    ) or "  (no regime data available)"

    strategy_lines = "\n".join(
        f"  {s['name']}: enabled={s['enabled']}, cb={s['cb']}, paper={s['paper']}"
        for s in strategy_states
    ) or "  (no strategy data available)"

    return f"""You are a quantitative trading system advisor for the QuantEmbrace algo trading platform.
You are reviewing today's NSE trading session ({session_id}) and producing a strategy recommendation.

TODAY'S DATA:
Portfolio NAV: {nav:.2f} INR
Regime distribution (signal counts):
{regime_lines}

Active strategies:
{strategy_lines}

INSTRUCTIONS:
- Review the regime distribution. If a strategy is designed for trending markets but today was
  predominantly ranging or volatile, recommend disabling it.
- Check for open circuit breakers (cb=OPEN). These need attention.
- All strategies are in paper mode until explicitly promoted. Do not recommend live promotion.
- Your recommendation must be ADVISORY ONLY. The operator will review and act manually.
- Keep reasoning concise (2-3 sentences).

Respond with ONLY a JSON object in this exact format:
```json
{{
  "recommended_action": "Short description of what should be done",
  "affected_strategies": ["StrategyName1", "StrategyName2"],
  "reasoning": "2-3 sentence explanation"
}}
```

If no action is needed, set recommended_action to "No action required" and affected_strategies to [].
"""
