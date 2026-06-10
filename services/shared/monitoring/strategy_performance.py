"""Strategy performance analysis for paper trading sessions."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

_IST = timezone(timedelta(hours=5, minutes=30))

# IST minute thresholds for time-bucket classification
_BUCKET_BOUNDARIES = [
    (9 * 60 + 15,  9 * 60 + 30,  "09:15–09:30"),
    (9 * 60 + 30,  10 * 60,      "09:30–10:00"),
    (10 * 60,      11 * 60,      "10:00–11:00"),
    (11 * 60,      13 * 60,      "11:00–13:00"),
    (13 * 60,      15 * 60,      "13:00–15:00"),
]


def _ist_minute(dt: datetime) -> int:
    """Return minutes-since-midnight in IST for the given datetime."""
    if dt.tzinfo is not None:
        ist = dt.astimezone(_IST)
    else:
        # Assume UTC if no tzinfo (DynamoDB ISO strings may omit tz)
        ist = dt.replace(tzinfo=timezone.utc).astimezone(_IST)
    return ist.hour * 60 + ist.minute


def _time_bucket(created_at_str: str) -> str:
    """Map an ISO timestamp string to the appropriate IST time bucket label."""
    try:
        # Handle both Z-suffix and +HH:MM offset forms
        ts = created_at_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        minute = _ist_minute(dt)
        for start, end, label in _BUCKET_BOUNDARIES:
            if start <= minute < end:
                return label
        return "other"
    except Exception:
        return "unknown"


# ── Trade-level record ────────────────────────────────────────────────────────


@dataclass
class TradeRecord:
    """One filled + exited order as read from the DynamoDB orders table."""

    symbol: str
    strategy_id: str
    side: str           # BUY / SELL
    direction: str      # LONG / SHORT
    filled_quantity: float
    filled_price: float
    exit_reason: str    # STOP_LOSS / TAKE_PROFIT / MIS_SQUARE_OFF / MANUAL / UNKNOWN
    pnl: float
    created_at_str: str
    confidence_score: Optional[float] = None
    reward_risk_ratio: Optional[float] = None

    @property
    def time_bucket(self) -> str:
        return _time_bucket(self.created_at_str)

    @property
    def confidence_band(self) -> str:
        """Map confidence_score to a 0.10-wide band label."""
        if self.confidence_score is None:
            return "unknown"
        c = self.confidence_score
        if c < 0.70:
            return "0.60-0.70"
        elif c < 0.80:
            return "0.70-0.80"
        elif c < 0.90:
            return "0.80-0.90"
        else:
            return "0.90-1.00"

    @property
    def rr_band(self) -> str:
        """Map reward_risk_ratio to a labelled band."""
        if self.reward_risk_ratio is None:
            return "unknown"
        rr = self.reward_risk_ratio
        if rr < 1.0:
            return "<1.0"
        elif rr < 1.5:
            return "1.0-1.5"
        elif rr < 2.0:
            return "1.5-2.0"
        else:
            return ">=2.0"


# ── Aggregate metric dataclasses ──────────────────────────────────────────────


@dataclass
class StrategyMetrics:
    strategy_id: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    sl_count: int = 0
    tp_count: int = 0
    realized_pnl: float = 0.0
    avg_holding_minutes: float = 0.0


@dataclass
class SymbolMetrics:
    symbol: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    expectancy: float = 0.0
    sl_count: int = 0
    tp_count: int = 0
    realized_pnl: float = 0.0
    avg_holding_minutes: float = 0.0


@dataclass
class TimeBucketMetrics:
    bucket_label: str
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    realized_pnl: float = 0.0
    sl_count: int = 0
    tp_count: int = 0


@dataclass
class ExitReasonMetrics:
    exit_reason: str
    count: int = 0
    total_pnl: float = 0.0
    avg_pnl: float = 0.0


@dataclass
class ConfidenceBandMetrics:
    band_label: str        # e.g. "0.90-1.00"
    total_trades: int = 0
    winning_trades: int = 0
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    avg_pnl: float = 0.0


@dataclass
class RRBandMetrics:
    band_label: str        # e.g. "1.0-1.5"
    total_trades: int = 0
    winning_trades: int = 0
    realized_pnl: float = 0.0
    win_rate: float = 0.0
    avg_pnl: float = 0.0


# ── Top-level performance status ──────────────────────────────────────────────


@dataclass
class StrategyPerformanceStatus:
    realized_pnl: float = 0.0
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0          # 0.0–1.0
    sl_tp_ratio: float = 0.0       # SL count / TP count (float('inf') if tp=0)
    profit_factor: float = 0.0     # gross_wins / abs(gross_losses)
    expectancy: float = 0.0        # (win_rate * avg_win) - (loss_rate * avg_loss)
    worst_strategy: str = "—"
    best_strategy: str = "—"
    worst_symbol: str = "—"
    best_symbol: str = "—"
    per_strategy: list = field(default_factory=list)        # list[StrategyMetrics]
    per_symbol: list = field(default_factory=list)          # list[SymbolMetrics]
    per_time_bucket: list = field(default_factory=list)     # list[TimeBucketMetrics]
    per_exit_reason: list = field(default_factory=list)     # list[ExitReasonMetrics]
    per_confidence_band: list = field(default_factory=list) # list[ConfidenceBandMetrics]
    per_rr_band: list = field(default_factory=list)         # list[RRBandMetrics]
    top_losers: list = field(default_factory=list)          # list[TradeRecord], top 10 by pnl asc
    top_winners: list = field(default_factory=list)         # list[TradeRecord], top 10 by pnl desc
    gate_status: str = "UNKNOWN"   # PASS / WARN / FAIL / UNKNOWN
    gate_reason: str = ""

    @property
    def gate_pass(self) -> bool:
        return self.realized_pnl > 0 and self.expectancy > 0 and self.profit_factor > 1.2


# ── Analyzer ──────────────────────────────────────────────────────────────────


class StrategyPerformanceAnalyzer:
    """
    Reads today's filled orders from DynamoDB and computes session-level
    strategy performance metrics without any broker or trading side effects.
    """

    def __init__(
        self,
        dynamo_client: Any,
        orders_table: str,
        risk_state_table: str,
        trade_date: Optional[str] = None,
    ) -> None:
        self._dynamo = dynamo_client
        self._orders_table = orders_table
        self._risk_state_table = risk_state_table
        # Default to today in IST
        if trade_date is None:
            self._trade_date = datetime.now(_IST).strftime("%Y-%m-%d")
        else:
            self._trade_date = trade_date

    async def analyze(self) -> StrategyPerformanceStatus:
        """Main entry point: scan orders, compute and return performance status."""
        records = await self._scan_filled_orders()
        return self._compute_metrics(records)

    async def _scan_filled_orders(self) -> list[TradeRecord]:
        """Paginated scan of the orders table for today's FILLED orders."""
        items: list[dict] = []
        kwargs: dict[str, Any] = dict(
            TableName=self._orders_table,
            FilterExpression=(
                "(order_status = :filled OR order_status = :paper_filled)"
                " AND trade_date = :td"
                " AND NOT begins_with(signal_id, :exit_prefix)"
            ),
            ExpressionAttributeValues={
                ":filled":       {"S": "FILLED"},
                ":paper_filled": {"S": "PAPER_FILLED"},
                ":td":           {"S": self._trade_date},
                ":exit_prefix":  {"S": "EXIT-"},
            },
        )
        try:
            while True:
                resp = await asyncio.to_thread(self._dynamo.scan, **kwargs)
                items.extend(resp.get("Items", []))
                last = resp.get("LastEvaluatedKey")
                if not last:
                    break
                kwargs["ExclusiveStartKey"] = last
        except Exception:
            # Degrade gracefully — caller gets empty analysis rather than a crash
            return []

        records: list[TradeRecord] = []
        for item in items:
            record = self._parse_order_item(item)
            if record is not None:
                records.append(record)
        return records

    def _parse_order_item(self, item: dict) -> Optional[TradeRecord]:
        """Parse a raw DynamoDB orders item into a TradeRecord. Returns None if pnl is absent."""
        def _s(key: str, default: str = "") -> str:
            raw = item.get(key, {})
            if isinstance(raw, dict):
                return raw.get("S", raw.get("N", default))
            return str(raw) if raw is not None else default

        def _n(key: str, default: float = 0.0) -> float:
            raw = item.get(key, {})
            if isinstance(raw, dict):
                v = raw.get("N")
                if v is not None:
                    try:
                        return float(v)
                    except ValueError:
                        return default
            return default

        # Skip items that have no pnl — they are unfilled or unexited
        if "realized_pnl" not in item:
            return None

        exit_reason = _s("exit_reason", "UNKNOWN")
        # Normalize exit_reason to known set
        if exit_reason not in ("STOP_LOSS", "TAKE_PROFIT", "MIS_SQUARE_OFF", "MANUAL"):
            exit_reason = "UNKNOWN"

        side = _s("side", "BUY").upper()
        direction = "LONG" if side == "BUY" else "SHORT"

        # Extract analytics fields from metadata JSON blob if present
        confidence_score: Optional[float] = None
        reward_risk_ratio: Optional[float] = None
        raw_meta = item.get("metadata", {})
        meta_str = raw_meta.get("S", "") if isinstance(raw_meta, dict) else str(raw_meta)
        if meta_str:
            try:
                meta = json.loads(meta_str)
                cs = meta.get("confidence_score")
                rr = meta.get("reward_risk_ratio")
                if cs is not None:
                    confidence_score = float(cs)
                if rr is not None:
                    reward_risk_ratio = float(rr)
            except (json.JSONDecodeError, ValueError):
                pass

        return TradeRecord(
            symbol=_s("symbol", "UNKNOWN"),
            strategy_id=_s("strategy_id", "unknown"),
            side=side,
            direction=direction,
            filled_quantity=_n("filled_quantity"),
            filled_price=_n("filled_price"),
            exit_reason=exit_reason,
            pnl=_n("realized_pnl"),
            created_at_str=_s("created_at"),
            confidence_score=confidence_score,
            reward_risk_ratio=reward_risk_ratio,
        )

    @staticmethod
    def _compute_metrics(records: list[TradeRecord]) -> StrategyPerformanceStatus:
        """
        Pure computation — no IO. Converts a flat list of TradeRecords into
        StrategyPerformanceStatus with all aggregate breakdowns.
        """
        if not records:
            return StrategyPerformanceStatus(gate_status="UNKNOWN", gate_reason="no trades")

        winners = [r for r in records if r.pnl > 0]
        losers  = [r for r in records if r.pnl < 0]
        sl_records = [r for r in records if r.exit_reason == "STOP_LOSS"]
        tp_records = [r for r in records if r.exit_reason == "TAKE_PROFIT"]

        total = len(records)
        win_count = len(winners)
        loss_count = len(losers)
        sl_count = len(sl_records)
        tp_count = len(tp_records)

        win_rate = win_count / total if total > 0 else 0.0
        loss_rate = 1.0 - win_rate

        avg_win  = sum(r.pnl for r in winners) / win_count  if win_count  > 0 else 0.0
        avg_loss = abs(sum(r.pnl for r in losers) / loss_count) if loss_count > 0 else 0.0

        gross_wins   = sum(r.pnl for r in winners)
        gross_losses = abs(sum(r.pnl for r in losers))

        # Profit factor is undefined (not 0) when there are no losses
        profit_factor = (gross_wins / gross_losses) if gross_losses > 0 else 0.0

        expectancy = (win_rate * avg_win) - (loss_rate * avg_loss)

        realized_pnl = sum(r.pnl for r in records)

        sl_tp_ratio = (
            float("inf") if tp_count == 0
            else sl_count / tp_count
        )

        # ── Per-strategy breakdown ─────────────────────────────────────────────
        strat_map: dict[str, list[TradeRecord]] = {}
        for r in records:
            strat_map.setdefault(r.strategy_id, []).append(r)

        per_strategy: list[StrategyMetrics] = []
        for sid, recs in strat_map.items():
            per_strategy.append(StrategyPerformanceAnalyzer._metrics_for_group(sid, recs, StrategyMetrics))

        # ── Per-symbol breakdown ───────────────────────────────────────────────
        sym_map: dict[str, list[TradeRecord]] = {}
        for r in records:
            sym_map.setdefault(r.symbol, []).append(r)

        per_symbol: list[SymbolMetrics] = []
        for sym, recs in sym_map.items():
            per_symbol.append(StrategyPerformanceAnalyzer._metrics_for_group(sym, recs, SymbolMetrics))

        # ── Per-time-bucket breakdown ──────────────────────────────────────────
        bucket_map: dict[str, list[TradeRecord]] = {}
        for r in records:
            bucket_map.setdefault(r.time_bucket, []).append(r)

        per_time_bucket: list[TimeBucketMetrics] = []
        for label, recs in sorted(bucket_map.items()):
            tb_wins = [x for x in recs if x.pnl > 0]
            tb_sl   = [x for x in recs if x.exit_reason == "STOP_LOSS"]
            tb_tp   = [x for x in recs if x.exit_reason == "TAKE_PROFIT"]
            per_time_bucket.append(TimeBucketMetrics(
                bucket_label=label,
                total_trades=len(recs),
                winning_trades=len(tb_wins),
                losing_trades=len(recs) - len(tb_wins),
                realized_pnl=sum(x.pnl for x in recs),
                sl_count=len(tb_sl),
                tp_count=len(tb_tp),
            ))

        # ── Per-exit-reason breakdown ──────────────────────────────────────────
        exit_map: dict[str, list[TradeRecord]] = {}
        for r in records:
            exit_map.setdefault(r.exit_reason, []).append(r)

        per_exit_reason: list[ExitReasonMetrics] = []
        for reason, recs in exit_map.items():
            total_pnl = sum(x.pnl for x in recs)
            per_exit_reason.append(ExitReasonMetrics(
                exit_reason=reason,
                count=len(recs),
                total_pnl=total_pnl,
                avg_pnl=total_pnl / len(recs),
            ))

        # ── Best / worst strategy and symbol ──────────────────────────────────
        worst_strategy = "—"
        best_strategy  = "—"
        if per_strategy:
            worst_strategy = min(per_strategy, key=lambda m: m.realized_pnl).strategy_id
            best_strategy  = max(per_strategy, key=lambda m: m.realized_pnl).strategy_id

        worst_symbol = "—"
        best_symbol  = "—"
        if per_symbol:
            worst_symbol = min(per_symbol, key=lambda m: m.realized_pnl).symbol
            best_symbol  = max(per_symbol, key=lambda m: m.realized_pnl).symbol

        top_losers  = sorted(records, key=lambda r: r.pnl)[:10]
        top_winners = sorted(records, key=lambda r: r.pnl, reverse=True)[:10]

        # ── Confidence band breakdown ──────────────────────────────────────────
        conf_band_map: dict[str, list[TradeRecord]] = {}
        for r in records:
            conf_band_map.setdefault(r.confidence_band, []).append(r)

        per_confidence_band: list[ConfidenceBandMetrics] = []
        for band_label in ["0.60-0.70", "0.70-0.80", "0.80-0.90", "0.90-1.00", "unknown"]:
            recs = conf_band_map.get(band_label, [])
            if not recs:
                continue
            band_wins = [x for x in recs if x.pnl > 0]
            band_pnl = sum(x.pnl for x in recs)
            per_confidence_band.append(ConfidenceBandMetrics(
                band_label=band_label,
                total_trades=len(recs),
                winning_trades=len(band_wins),
                realized_pnl=band_pnl,
                win_rate=len(band_wins) / len(recs) if recs else 0.0,
                avg_pnl=band_pnl / len(recs) if recs else 0.0,
            ))

        # ── R:R band breakdown ─────────────────────────────────────────────────
        rr_band_map: dict[str, list[TradeRecord]] = {}
        for r in records:
            rr_band_map.setdefault(r.rr_band, []).append(r)

        per_rr_band: list[RRBandMetrics] = []
        for band_label in ["<1.0", "1.0-1.5", "1.5-2.0", ">=2.0", "unknown"]:
            recs = rr_band_map.get(band_label, [])
            if not recs:
                continue
            band_wins = [x for x in recs if x.pnl > 0]
            band_pnl = sum(x.pnl for x in recs)
            per_rr_band.append(RRBandMetrics(
                band_label=band_label,
                total_trades=len(recs),
                winning_trades=len(band_wins),
                realized_pnl=band_pnl,
                win_rate=len(band_wins) / len(recs) if recs else 0.0,
                avg_pnl=band_pnl / len(recs) if recs else 0.0,
            ))

        # ── Gate evaluation ───────────────────────────────────────────────────
        if realized_pnl < 0 and expectancy < 0:
            gate_status = "FAIL"
            gate_reason = f"realized_pnl={realized_pnl:.2f} < 0 and expectancy={expectancy:.2f} < 0"
        elif realized_pnl < 0 and expectancy >= 0:
            gate_status = "WARN"
            gate_reason = f"realized_pnl={realized_pnl:.2f} < 0 but expectancy={expectancy:.2f} >= 0 (improving)"
        elif realized_pnl > 0 and expectancy > 0 and profit_factor > 1.2:
            gate_status = "PASS"
            gate_reason = f"realized_pnl={realized_pnl:.2f} > 0, expectancy={expectancy:.2f} > 0, pf={profit_factor:.2f} > 1.2"
        else:
            gate_status = "WARN"
            gate_reason = f"pnl={realized_pnl:.2f}, expectancy={expectancy:.2f}, pf={profit_factor:.2f} — does not meet PASS criteria"

        return StrategyPerformanceStatus(
            realized_pnl=realized_pnl,
            total_trades=total,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=win_rate,
            sl_tp_ratio=sl_tp_ratio,
            profit_factor=profit_factor,
            expectancy=expectancy,
            worst_strategy=worst_strategy,
            best_strategy=best_strategy,
            worst_symbol=worst_symbol,
            best_symbol=best_symbol,
            per_strategy=per_strategy,
            per_symbol=per_symbol,
            per_time_bucket=per_time_bucket,
            per_exit_reason=per_exit_reason,
            per_confidence_band=per_confidence_band,
            per_rr_band=per_rr_band,
            top_losers=top_losers,
            top_winners=top_winners,
            gate_status=gate_status,
            gate_reason=gate_reason,
        )

    @staticmethod
    def _metrics_for_group(key: str, recs: list[TradeRecord], cls: type) -> Any:
        """Compute shared win/loss/SL/TP metrics for a list of trades keyed by strategy or symbol."""
        wins   = [r for r in recs if r.pnl > 0]
        losses = [r for r in recs if r.pnl < 0]
        sls    = [r for r in recs if r.exit_reason == "STOP_LOSS"]
        tps    = [r for r in recs if r.exit_reason == "TAKE_PROFIT"]

        total = len(recs)
        win_count  = len(wins)
        loss_count = len(losses)

        win_rate  = win_count / total if total > 0 else 0.0
        loss_rate = 1.0 - win_rate
        avg_win   = sum(r.pnl for r in wins)  / win_count  if win_count  > 0 else 0.0
        avg_loss  = abs(sum(r.pnl for r in losses) / loss_count) if loss_count > 0 else 0.0

        gross_wins   = sum(r.pnl for r in wins)
        gross_losses = abs(sum(r.pnl for r in losses))
        pf           = (gross_wins / gross_losses) if gross_losses > 0 else 0.0
        expectancy   = (win_rate * avg_win) - (loss_rate * avg_loss)
        realized_pnl = sum(r.pnl for r in recs)

        # The key field name differs between StrategyMetrics (strategy_id) and SymbolMetrics (symbol)
        if cls is StrategyMetrics:
            return StrategyMetrics(
                strategy_id=key,
                total_trades=total,
                winning_trades=win_count,
                losing_trades=loss_count,
                win_rate=win_rate,
                avg_win=avg_win,
                avg_loss=avg_loss,
                profit_factor=pf,
                expectancy=expectancy,
                sl_count=len(sls),
                tp_count=len(tps),
                realized_pnl=realized_pnl,
            )
        return SymbolMetrics(
            symbol=key,
            total_trades=total,
            winning_trades=win_count,
            losing_trades=loss_count,
            win_rate=win_rate,
            avg_win=avg_win,
            avg_loss=avg_loss,
            profit_factor=pf,
            expectancy=expectancy,
            sl_count=len(sls),
            tp_count=len(tps),
            realized_pnl=realized_pnl,
        )
