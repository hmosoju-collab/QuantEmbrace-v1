"""Deterministic trade review: every classification is computed by code.

Rules (documented thresholds, not model judgement):
  * quant_thesis   — the book bought the name to beat its EW liquid benchmark:
                     CONFIRMED if excess return > 0, else REFUTED.
  * ai_thesis      — only if a research signal existed at entry: sign(ai_score)
                     (±0.20 dead zone) vs sign(excess) → CONFIRMED / REFUTED /
                     NEUTRAL; NO_SIGNAL otherwise.
  * entry_quality  — where the entry price sits in the close range of the first
                     10 held sessions: bottom third GOOD, top third POOR.
  * exit_quality   — share of the maximum favourable excursion captured:
                     ≥ 2/3 GOOD, < 1/3 POOR; never in profit → POOR if exit
                     ≤ -5% vs entry, else NEUTRAL.
  * regime_accuracy — PIT regime at entry vs the EW benchmark's sign over the
                     hold: RISK_ON & up or RISK_OFF & down → CORRECT.
  * execution_quality — round-trip cost / buy notional: ≤ 40 bps GOOD,
                     ≤ 80 bps NEUTRAL, else POOR.
  * risk_event     — MAE ≤ -15% during the hold.
Everything uses closes up to the trade's close date, so the review is
knowable exactly at that close (``knowledge_ts``).
"""

from datetime import date, datetime
import hashlib
from typing import Annotated, Literal

import numpy as np
from pydantic import Field

from qe.ai.models.common import ComponentStatus
from qe.ai.post_trade.trades import Trade
from qe.ai.tools import ResearchDataAPI, knowledge_ts, regime
from qe.config import FrozenModel
from qe.data.panel import Panel
from qe.universe import liquid_universe

Quality = Literal["GOOD", "NEUTRAL", "POOR", "UNKNOWN"]
ENTRY_WINDOW = 10
DEAD_ZONE = 0.20


class PostTradeReview(FrozenModel):
    schema_version: Literal["post_trade_review/1"] = "post_trade_review/1"
    review_id: str
    source_session: str
    symbol: str
    open_date: date
    close_date: date
    qty_bought: int
    entry_price: float
    exit_price: float
    gross_return: float
    net_return: float
    benchmark_return: float | None
    excess_return: float | None
    mfe: float
    mae: float
    cost_bps: float
    regime_at_entry: str
    regime_accuracy: Literal["CORRECT", "INCORRECT", "UNKNOWN"]
    entry_quality: Quality
    exit_quality: Quality
    execution_quality: Quality
    quant_thesis: Literal["CONFIRMED", "REFUTED", "UNKNOWN"]
    ai_thesis: Literal["CONFIRMED", "REFUTED", "NEUTRAL", "NO_SIGNAL"]
    ai_score_at_entry: float | None = None
    ai_contaminated: bool | None = None
    risk_event: str = ""
    # LLM narrative (advisory; never feeds a classification)
    unexpected_event: Annotated[str, Field(max_length=240)] = ""
    lesson: Annotated[str, Field(max_length=400)] = ""
    lesson_status: ComponentStatus
    knowledge_ts: datetime
    model_id: str | None = None
    prompt_version: str | None = None


def review_id(trade: Trade) -> str:
    key = f"{trade.session_id}|{trade.symbol}|{trade.open_date}|{trade.close_date}"
    return "ptr-" + hashlib.sha256(key.encode()).hexdigest()[:12]


def _pos(panel: Panel, d: date) -> int | None:
    for i in range(len(panel.index) - 1, -1, -1):
        if panel.date_at(i) <= d:
            return i if panel.date_at(i) == d else None
    return None


def facts(trade: Trade, panel: Panel, market: str, top_n: int, ai_score: float | None) -> dict:
    """All deterministic facts + classifications for one completed trade."""
    p0, p1 = _pos(panel, trade.open_date), _pos(panel, trade.close_date)
    if p0 is None or p1 is None or p1 <= p0:
        raise ValueError(f"{trade.symbol}: trade dates not in the panel")
    px = panel.close[trade.symbol].iloc[p0 : p1 + 1] if trade.symbol in panel.close else None
    if px is None or px.dropna().empty:
        raise ValueError(f"{trade.symbol}: no prices over the hold")
    entry, exit_ = trade.entry_price, trade.exit_price
    held = px.iloc[1:].dropna()
    mfe = float(held.max() / entry - 1.0) if len(held) else 0.0
    mae = float(held.min() / entry - 1.0) if len(held) else 0.0

    members = liquid_universe(panel.close, panel.turnover, p0, top_n)
    rets = (panel.close[members].iloc[p1] / panel.close[members].iloc[p0] - 1.0).dropna()
    bench = float(rets.mean()) if len(rets) else None
    excess = trade.net_return - bench if bench is not None else None

    window = px.iloc[1 : 1 + ENTRY_WINDOW].dropna()
    if len(window) >= 3 and window.max() > window.min():
        where = (entry - window.min()) / (window.max() - window.min())
        entry_q: Quality = "GOOD" if where <= 1 / 3 else "POOR" if where >= 2 / 3 else "NEUTRAL"
    else:
        entry_q = "UNKNOWN"

    peak = entry * (1.0 + mfe)
    if peak > entry:
        capture = (exit_ - entry) / (peak - entry)
        exit_q: Quality = "GOOD" if capture >= 2 / 3 else "POOR" if capture < 1 / 3 else "NEUTRAL"
    else:
        exit_q = "POOR" if exit_ / entry - 1.0 <= -0.05 else "NEUTRAL"

    reading = regime(ResearchDataAPI(panel, p0, market))[1]
    if reading.label == "UNKNOWN" or bench is None:
        regime_acc = "UNKNOWN"
    else:
        regime_acc = "CORRECT" if (reading.label == "RISK_ON") == (bench > 0) else "INCORRECT"

    cost_bps = float(trade.costs / trade.buy_notional * 1e4)
    exec_q: Quality = "GOOD" if cost_bps <= 40 else "NEUTRAL" if cost_bps <= 80 else "POOR"
    quant_thesis = "UNKNOWN" if excess is None else "CONFIRMED" if excess > 0 else "REFUTED"
    if ai_score is None or excess is None:
        ai_thesis = "NO_SIGNAL"
    elif abs(ai_score) <= DEAD_ZONE:
        ai_thesis = "NEUTRAL"
    else:
        ai_thesis = "CONFIRMED" if (ai_score > 0) == (excess > 0) else "REFUTED"

    return {
        "gross_return": float(exit_ / entry - 1.0),
        "net_return": float(trade.net_return),
        "benchmark_return": bench,
        "excess_return": excess,
        "mfe": mfe,
        "mae": mae,
        "cost_bps": cost_bps,
        "regime_at_entry": reading.label,
        "regime_accuracy": regime_acc,
        "entry_quality": entry_q,
        "exit_quality": exit_q,
        "execution_quality": exec_q,
        "quant_thesis": quant_thesis,
        "ai_thesis": ai_thesis,
        "risk_event": f"MAE {mae:.1%} during the hold" if mae <= -0.15 else "",
        "holding_days": (trade.close_date - trade.open_date).days,
        "knowledge_ts": knowledge_ts(trade.close_date, market),
    }


def nan_safe(v):
    return None if isinstance(v, float) and np.isnan(v) else v
