"""EodRollupTask — end-of-day per-model performance rollups (ADR-031).

At 15:35 IST (after the MIS square-off window, market closed) computes, per
``model_id@model_version`` x horizon: rank-IC, hit rate, avg net edge, avg
realized bps, calibration buckets, and per-universe breakdown over the day's
labeled forecasts, then writes one row per group to the performance store.

Drift (P4.5) and the health state (P5.5) are layered onto the same rollup.
"""

from __future__ import annotations

import pandas as pd

from alpha_engine.research import alpha_metrics as M
from alpha_engine.store.forecast_store import ForecastStore
from alpha_engine.store.performance_store import PerformanceStore
from shared.logging.logger import get_logger

logger = get_logger(__name__, service_name="alpha_engine")


def _universe_breakdown(df: pd.DataFrame) -> dict:
    out: dict[str, dict] = {}
    if "universe" not in df.columns:
        return out
    for universe, grp in df.groupby("universe"):
        out[str(universe)] = {
            "n": int(len(grp[grp[M.REALIZED_COL].notna()])),
            "ic": M.rank_ic(grp)["ic"],
            "hit_rate": M.hit_rate(grp),
        }
    return out


class EodRollupTask:
    def __init__(
        self,
        forecast_store: ForecastStore,
        performance_store: PerformanceStore,
        *,
        market: str = "NSE",
    ) -> None:
        self._store = forecast_store
        self._perf = performance_store
        self._market = market

    def run(self, trade_date: str) -> int:
        """Compute + persist rollups for one IST trading day. Returns rows written."""
        rows = self._store.query_day(trade_date, self._market)
        if not rows:
            logger.info("alpha_engine.eod_rollup_empty date=%s", trade_date)
            return 0
        df = pd.DataFrame(rows)
        written = 0
        group_cols = ["model_id", "model_version", "horizon_minutes"]
        for (model_id, model_version, horizon), grp in df.groupby(group_cols):
            payload = self._rollup_payload(grp)
            self._perf.put_rollup(
                model_id=str(model_id),
                model_version=str(model_version),
                trade_date=trade_date,
                horizon_minutes=int(horizon),
                payload=payload,
            )
            written += 1
        logger.info("alpha_engine.eod_rollup_done date=%s rows=%d", trade_date, written)
        return written

    @staticmethod
    def _rollup_payload(grp: pd.DataFrame) -> dict:
        labeled = grp[grp[M.REALIZED_COL].notna()]
        calib = M.calibration_curve(grp)
        return {
            "forecast_count": int(len(grp)),
            "published_count": int(grp.get("published", pd.Series(dtype=bool)).fillna(False).sum())
            if "published" in grp.columns else 0,
            "suppressed_count": int((grp.get("suppressed_by", pd.Series(dtype=str)).fillna("") != "").sum())
            if "suppressed_by" in grp.columns else 0,
            "labeled_count": int(len(labeled)),
            "unlabelable_count": int((grp.get("label_status", pd.Series(dtype=str)) == "UNLABELABLE").sum())
            if "label_status" in grp.columns else 0,
            "rank_ic": M.rank_ic(grp)["ic"],
            "hit_rate": M.hit_rate(grp),
            "avg_net_edge_bps": float(grp["net_edge_bps"].mean()) if "net_edge_bps" in grp.columns else None,
            "avg_realized_bps": float(M.signed_realized_bps(labeled).mean()) if len(labeled) else None,
            "calibration_buckets": calib.to_dict(orient="records"),
            "universe_breakdown": _universe_breakdown(grp),
        }
