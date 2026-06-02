"""Broker heartbeat collector — inferred from persisted state, never the broker.

A Phase-1, read-only agent must not call the broker API: that would require live
credentials, could hit rate limits, and could have side-effects. Instead we infer
broker connectivity from state the platform already persists in DynamoDB —
specifically the freshness of the ``latest-prices`` table, which the data
ingestion service updates from the broker's market-data feed.

Logic (market-hours aware to avoid false alarms overnight/weekends):

    * cannot read latest-prices                       → UNKNOWN
    * market closed                                    → OK (idle feed expected)
    * market open, newest price <= max_age             → OK (feed live)
    * market open, newest price >  max_age             → DEGRADED (feed likely stale)
    * market open, table empty                         → DEGRADED (no prices at all)

It reads a small, projection-limited sample (cheap RCU) and never touches the
credential/token item, so no secret is ever read or logged. Tunables come from a
``broker:`` block in rules.yaml (all optional, safe defaults below).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from monitoring_agent.collectors.base import Collector
from monitoring_agent.snapshot import CollectorResult, Status

logger = logging.getLogger("monitoring_agent.collectors.broker")

_DEFAULTS = {
    "latest_prices_suffix": "latest-prices",
    "timestamp_attr": "timestamp",
    "max_age_seconds": 60.0,
    "sample_limit": 50,
    "market_hours_aware": True,
}


def _parse_iso_age_seconds(value: Any, now: datetime) -> Optional[float]:
    """Return age in seconds for an ISO-8601 timestamp, or None if unparseable."""
    if not isinstance(value, str) or not value:
        return None
    text = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return (now - parsed).total_seconds()


class BrokerCollector(Collector):
    """Infer broker feed health from latest-prices freshness (no broker calls)."""

    name = "broker"

    def _cfg(self) -> dict[str, Any]:
        raw = self.rules.raw.get("broker") if isinstance(self.rules.raw, dict) else None
        raw = raw if isinstance(raw, dict) else {}
        cfg = dict(_DEFAULTS)
        for key, default in _DEFAULTS.items():
            if key in raw:
                try:
                    cfg[key] = type(default)(raw[key])
                except (TypeError, ValueError):
                    cfg[key] = default
        return cfg

    def _market_open(self, aware: bool) -> bool:
        if not aware:
            return True  # treat every cycle as "open" → staleness always meaningful
        try:
            from shared.zerodha.market_phase import MarketPhaseGovernor

            return bool(MarketPhaseGovernor().is_market_open())
        except Exception:  # noqa: BLE001 — fail toward visibility, not silence
            return True

    def _collect_sync(self) -> CollectorResult:
        cfg = self._cfg()
        try:
            from shared.aws.clients import get_dynamodb_resource
        except Exception as exc:  # noqa: BLE001
            return self._result(Status.UNKNOWN, "broker: boto3/shared client unavailable", error=repr(exc))

        table_name = self.config.table(cfg["latest_prices_suffix"])
        ts_attr = cfg["timestamp_attr"]
        try:
            resource = get_dynamodb_resource()
            table = resource.Table(table_name)
            resp = table.scan(
                ProjectionExpression="#s, #t",
                ExpressionAttributeNames={"#s": "symbol", "#t": ts_attr},
                Limit=int(cfg["sample_limit"]),
            )
            items = resp.get("Items", []) or []
        except Exception as exc:  # noqa: BLE001
            return self._result(
                Status.UNKNOWN,
                f"broker: cannot read {table_name} (price feed state unknown)",
                error=repr(exc),
            )

        now = datetime.now(timezone.utc)
        market_open = self._market_open(bool(cfg["market_hours_aware"]))
        ages = [
            age
            for age in (_parse_iso_age_seconds(it.get(ts_attr), now) for it in items)
            if age is not None
        ]
        newest_age = min(ages) if ages else None
        details: dict[str, Any] = {
            "table": table_name,
            "sampled": len(items),
            "parsed_timestamps": len(ages),
            "newest_age_seconds": round(newest_age, 1) if newest_age is not None else None,
            "max_age_seconds": cfg["max_age_seconds"],
            "market_open": market_open,
            "inferred_from": "latest-prices freshness (no broker API call)",
        }

        if not items:
            if market_open:
                return self._result(Status.DEGRADED, "broker: no recent prices while market open", details=details)
            return self._result(Status.OK, "broker: no prices (market closed — expected)", details=details)

        if newest_age is None:
            return self._result(Status.UNKNOWN, "broker: could not parse any price timestamps", details=details)

        max_age = float(cfg["max_age_seconds"])
        if market_open and newest_age > max_age:
            return self._result(
                Status.DEGRADED,
                f"broker: price feed stale — newest tick {newest_age:.0f}s old (> {max_age:.0f}s)",
                details=details,
            )

        descriptor = "fresh" if newest_age <= max_age else "stale but market closed"
        return self._result(
            Status.OK,
            f"broker: feed {descriptor} — newest tick {newest_age:.0f}s old",
            details=details,
        )
