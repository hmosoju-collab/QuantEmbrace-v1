#!/usr/bin/env python
"""run_alpha_replay — offline forecast generation from a candle Parquet (ADR-031).

Feeds historical 1m/15m candles through the production-strategy alpha models and
writes a forecasts Parquet plus a reproducibility manifest. Read-only research.

    python scripts/alpha/run_alpha_replay.py \\
        --candles candles.parquet --models alpha_orb_v2,alpha_vwap_rev_v2 \\
        --model-version 2026-06-13 --horizons 15,30,60 --out forecasts.parquet

``candles.parquet`` columns: symbol, market, open, high, low, close, volume,
timestamp, interval (e.g. "minute"/"15minute"). Sorted chronologically.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

# ── Path bootstrap ─────────────────────────────────────────────────────────────
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

import pandas as pd  # noqa: E402

from alpha_engine.research.manifest import DatasetSnapshot, ResearchManifest  # noqa: E402
from alpha_engine.research.replay import build_offline_models, replay_models  # noqa: E402


def _bars_from_df(df: pd.DataFrame):
    from strategy_engine.strategies.base_strategy import Bar

    bars = []
    for _, r in df.iterrows():
        bars.append(
            Bar(
                symbol=str(r["symbol"]),
                market=str(r.get("market", "NSE")),
                open=float(r["open"]),
                high=float(r["high"]),
                low=float(r["low"]),
                close=float(r["close"]),
                volume=int(r["volume"]),
                timestamp=pd.to_datetime(r["timestamp"]).to_pydatetime(),
                interval=str(r.get("interval", "minute")),
            )
        )
    return bars


async def _run(args: argparse.Namespace) -> int:
    raw = open(args.candles, "rb").read()
    candles = pd.read_parquet(args.candles)
    symbols = sorted(candles["symbol"].astype(str).unique().tolist())
    horizons = [int(h) for h in args.horizons.split(",")]
    model_ids = [m.strip() for m in args.models.split(",") if m.strip()]

    models = build_offline_models(
        model_ids, args.model_version, symbols=symbols, horizons_minutes=horizons
    )
    bars = _bars_from_df(candles.sort_values("timestamp"))
    forecasts = await replay_models(bars, models)
    forecasts.to_parquet(args.out, index=False)

    manifest = ResearchManifest.build(
        config={"models": model_ids, "horizons": horizons},
        model_versions=[args.model_version],
        horizons=horizons,
        dataset=DatasetSnapshot(dataset_id=args.dataset_id, dataset_hash=ResearchManifest.build.__self__ and "" or ""),
        inputs={"candles": raw},
    )
    # dataset_hash from the candle bytes
    manifest = ResearchManifest.build(
        config={"models": model_ids, "horizons": horizons},
        model_versions=[args.model_version],
        horizons=horizons,
        dataset=DatasetSnapshot(
            dataset_id=args.dataset_id,
            dataset_hash=__import__("hashlib").sha256(raw).hexdigest(),
        ),
        inputs={"candles": raw},
    )
    manifest.write(args.out + ".manifest.json")
    print(f"wrote {len(forecasts)} forecasts -> {args.out} (+ manifest)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Offline alpha forecast replay (ADR-031).")
    p.add_argument("--candles", required=True, help="Input candles Parquet.")
    p.add_argument("--models", required=True, help="Comma-separated alpha model ids.")
    p.add_argument("--model-version", required=True)
    p.add_argument("--horizons", default="15,30,60")
    p.add_argument("--dataset-id", default="unspecified")
    p.add_argument("--out", required=True, help="Output forecasts Parquet.")
    args = p.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
