"""Market regime from a genuinely point-in-time market proxy.

Deliberately NOT ``qe.research.wf_v1.regime_series``: that parity anchor picks
its top-100 from total-period turnover (look-ahead, current-state F-10). Here
membership is the liquid top-100 by trailing [t-60, t) turnover, and the
equal-weight proxy is compared with its own 200-day average.
"""

from dataclasses import dataclass
import math

from qe.ai.models import ComponentStatus
from qe.ai.tools.pit import ResearchDataAPI, ToolResult, evidence, unavailable
from qe.universe import liquid_universe

TOOL = "regime"
PROXY_N = 100
SMA = 200
WINDOW = 260  # rows of proxy history needed for a full SMA200
SCALE = 0.10  # proxy 10% above/below its SMA200 ⇒ |R| = 1


@dataclass(frozen=True)
class RegimeReading:
    label: str  # RISK_ON | RISK_OFF | UNKNOWN
    value: float | None  # R in [-1, 1]
    confidence: float | None


UNKNOWN = RegimeReading("UNKNOWN", None, None)


def regime(api: ResearchDataAPI) -> tuple[ToolResult, RegimeReading]:
    ctx = api.context()
    i = ctx.now_pos
    if i < WINDOW:
        return unavailable(TOOL, None, f"needs {WINDOW} rows of history"), UNKNOWN
    members = liquid_universe(ctx.close, ctx.turnover, i, PROXY_N)
    if len(members) < 10:
        return unavailable(TOOL, None, "too few liquid names for a market proxy"), UNKNOWN
    rets = ctx.close[members].iloc[i - WINDOW + 1 : i + 1].pct_change().mean(axis=1).fillna(0.0)
    idx = (1.0 + rets).cumprod()
    sma = idx.rolling(SMA).mean().iloc[-1]
    ratio = float(idx.iloc[-1] / sma - 1.0)
    r = max(-1.0, min(1.0, ratio / SCALE))
    reading = RegimeReading("RISK_ON" if ratio > 0 else "RISK_OFF", r, abs(r))

    ev = [
        evidence(
            api, TOOL, "proxy_vs_sma200", ratio, "EW liquid-100 proxy vs its 200-day SMA", None
        ),
        evidence(api, TOOL, "r_value", r, "regime value R in [-1, 1]", None),
        evidence(api, TOOL, "proxy_members", len(members), "names in the PIT market proxy", None),
    ]
    vix = api.index_series("INDIAVIX")
    if vix is not None and len(vix) >= 2:
        last = float(vix.iloc[-1])
        window = vix.iloc[-252:]
        pct = float((window <= last).mean())
        if not math.isnan(last):
            ev.append(evidence(api, TOOL, "indiavix", last, "India VIX close", None))
            ev.append(
                evidence(api, TOOL, "indiavix_pct_1y", pct, "VIX percentile vs trailing year", None)
            )
    return ToolResult(TOOL, None, ComponentStatus.OK, tuple(ev)), reading
