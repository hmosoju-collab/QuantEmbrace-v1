#!/usr/bin/env python3
"""End-to-end full-run orchestrator for the QuantEmbrace backtesting lab.

Runs the AWS-BT-12 run order across all implemented modules:
  1. 1-month smoke  2. 1-year  3. 5-year  4. 5-year (wider)  5. 10–15-year
  6. walk-forward   7. model dataset   8. GenAI report

Registers each run (registry), writes per-run reports (report_writer), compares
TEE policies, runs walk-forward, builds a model dataset, and generates a cited
GenAI summary — emitting a summary JSON for the phase report.

**Controlled DRY-RUN.** No real licensed 10–15-year NSE data is ingested and no
AWS infra is provisioned (Phase-0 blocker), so this runs on SYNTHETIC data to
prove the orchestration end-to-end. Results are NON-AUTHORITATIVE. Backtest-only:
no broker APIs, no live, no capital change. Never approves or enables live.
"""

from __future__ import annotations

import asyncio
import json
import random
import sys
from datetime import time
from pathlib import Path

import pandas as pd

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

from backtesting.metrics_engine import RunMeta, breakdowns, compute_metrics  # noqa: E402
from backtesting.model_dataset_builder import (  # noqa: E402
    DatasetMeta,
    ModelDatasetBuilder,
    SignalRecord,
)
from backtesting.replay_engine import (  # noqa: E402
    Candle,
    CandleReplayEngine,
    DataFrameBarSource,
    ReplayConfig,
)
from backtesting.report_writer import ReportWriter  # noqa: E402
from backtesting.run_registry import RunRegistry, RunSpec  # noqa: E402
from backtesting.strategy_adapter import get_adapter, list_adapters  # noqa: E402
from backtesting.tee_simulator import compare_policies  # noqa: E402
from backtesting.walk_forward import PRESETS, fold_report, run_walk_forward  # noqa: E402
from shared.models.signal import Direction  # noqa: E402

IST = "Asia/Kolkata"
CODE_VERSION = "aws-bt-12-dryrun"
DATA_VERSION = "SYNTHETIC-no-real-nse-data"


class _FakeTable:
    def __init__(self, name="qe-bt-runs"):
        self.name = name
        self._store = {}

    def put_item(self, Item, **kw):
        self._store[Item["run_id"]] = dict(Item)

    def get_item(self, Key, **kw):
        it = self._store.get(Key["run_id"])
        return {"Item": dict(it)} if it else {}

    def scan(self, **kw):
        return {"Items": [dict(v) for v in self._store.values()]}


def _syms(prefix: str, n: int) -> list[str]:
    return [f"{prefix}{i:02d}" for i in range(n)]


def _synth_daily(symbol: str, start: str, n: int, seed: int) -> list[Candle]:
    rng = random.Random(seed)
    t = pd.Timestamp(start, tz=IST)
    px = 100.0
    out = []
    for i in range(n):
        px = max(5.0, px + rng.uniform(-1.0, 1.06))  # slight upward drift
        o = px
        c = max(5.0, px + rng.uniform(-0.5, 0.5))
        h = max(o, c) + abs(rng.uniform(0, 0.6))
        low = min(o, c) - abs(rng.uniform(0, 0.6))
        out.append(Candle(symbol, "NSE", "EQ", "1d", t, o, h, low, c, 100000 + i))
        t = t + pd.Timedelta(days=1)
    return out


def _synth_intraday(symbol: str, start: str, n_days: int, seed: int, interval: str = "1m") -> list[Candle]:
    rng = random.Random(seed)
    step = {"1m": 1, "5m": 5, "15m": 15}[interval]
    out = []
    day = pd.Timestamp(start, tz=IST)
    px = 100.0
    for _ in range(n_days):
        t = pd.Timestamp(f"{day.date()} 09:15", tz=IST)
        while t.time() <= time(15, 30):
            px = max(5.0, px + rng.uniform(-0.4, 0.42))
            o = px
            c = max(5.0, px + rng.uniform(-0.3, 0.3))
            h = max(o, c) + abs(rng.uniform(0, 0.3))
            low = min(o, c) - abs(rng.uniform(0, 0.3))
            out.append(Candle(symbol, "NSE", "EQ", interval, t, o, h, low, c, 50000))
            t = t + pd.Timedelta(minutes=step)
        day = day + pd.Timedelta(days=1)
    return out


def _trades_df(results: dict, strategy: str) -> pd.DataFrame:
    rows = []
    for _pid, res in results.items():
        for tr in res.trades:
            rows.append({
                "symbol": tr.symbol, "strategy": strategy, "direction": tr.direction.value,
                "entry_time": tr.entry_time, "exit_time": tr.exit_time,
                "entry_price": tr.entry_price, "exit_price": tr.exit_price, "quantity": tr.quantity,
                "gross_pnl": tr.pnl + tr.commission, "costs": tr.commission, "slippage": tr.slippage,
                "net_pnl": tr.pnl, "exit_reason": tr.exit_reason,
                "mfe_r": 0.0, "mae_r": 0.0, "r_multiple": 0.0, "mis_dependent": False,
            })
    cols = ["symbol", "strategy", "direction", "entry_time", "exit_time", "entry_price",
            "exit_price", "quantity", "gross_pnl", "costs", "slippage", "net_pnl",
            "exit_reason", "mfe_r", "mae_r", "r_multiple", "mis_dependent"]
    return pd.DataFrame(rows, columns=cols)


def _run_universe(label, symbols, start, n_days, seed, registry, writer) -> dict:
    candles = []
    for i, s in enumerate(symbols):
        candles.extend(_synth_daily(s, start, n_days, seed + i))
    eng = CandleReplayEngine(DataFrameBarSource(candles),
                             ReplayConfig(timeframes=["1d"], partition_by="symbol", market_hours_filter=False))
    adapter = get_adapter("momentum")
    results = eng.run_with_backtester(
        lambda: adapter.build_strategy(symbols, short_window=5, long_window=20, min_confidence=0.0),
        backtester_kwargs={"slippage_bps": 2.0, "spread_bps": 4.0, "commission_pct": 0.03},
    )
    trades = _trades_df(results, "momentum")
    metrics = compute_metrics(trades, initial_capital=1_000_000)

    spec = RunSpec(strategy="momentum", symbols=symbols, timeframe="1d",
                   start_date=start, end_date=str(pd.Timestamp(start, tz=IST).date()),
                   config_s3_path=f"s3://quantembrace-backtest-results/runs/cfg/{label}.json",
                   code_version=CODE_VERSION, data_version=DATA_VERSION,
                   cost_model_version="indian-v1", exit_policy_version="tee@1.0", operator="aws-bt-12")
    rec = registry.create_run(spec)
    registry.mark_running(rec.run_id)
    registry.mark_completed(rec.run_id, result_s3_path=f"s3://quantembrace-backtest-results/runs/{rec.run_id}/")

    meta = RunMeta(run_id=rec.run_id, strategy="momentum", symbols=tuple(symbols), start_date=start,
                   end_date=spec.end_date, code_version=CODE_VERSION, data_version=DATA_VERSION,
                   cost_model_version="indian-v1", exit_policy_version="tee@1.0")
    writer.write_run(meta, metrics=metrics, trades=trades)

    per_symbol = trades.groupby("symbol")["net_pnl"].sum().sort_values(ascending=False) if not trades.empty else pd.Series(dtype=float)
    return {
        "label": label, "run_id": rec.run_id, "config_s3_path": spec.config_s3_path,
        "result_s3_path": f"s3://quantembrace-backtest-results/runs/{rec.run_id}/",
        "symbols": symbols, "n_days": n_days,
        "net_pnl": metrics["net_pnl"], "trades": metrics["number_of_trades"],
        "profit_factor": metrics["profit_factor"], "expectancy": metrics["expectancy"],
        "win_rate": metrics["win_rate"], "max_drawdown_pct": metrics["max_drawdown_pct"],
        "cost_impact": metrics["cost_impact"], "gates": metrics["gates"],
        "per_symbol_top": {k: round(float(v), 2) for k, v in per_symbol.head(5).items()},
    }


def _strategy_ranking() -> list[dict]:
    """Exercise all 6 adapters on a small intraday synthetic set (signal counts)."""
    out = []
    for name in list_adapters():
        a = get_adapter(name)
        bars = _synth_intraday("RANK", "2020-06-01", 5, seed=7, interval=a.replay_interval)
        sigs = asyncio.run(a.collect_signals(bars, ["RANK"], data_version=DATA_VERSION,
                                             short_window=3, long_window=14, min_confidence=0.0)
                           if name == "momentum"
                           else a.collect_signals(bars, ["RANK"], data_version=DATA_VERSION))
        out.append({"strategy": name, "interval": a.replay_interval,
                    "signals": len(sigs), "paper_only": a.paper_only,
                    "version": a.strategy_version})
    return sorted(out, key=lambda r: r["signals"], reverse=True)


def main() -> int:
    out_dir = _REPO / "reports" / "backtests"
    registry = RunRegistry(_FakeTable("qe-bt-runs"))
    writer = ReportWriter(base_dir=str(out_dir))

    print("Phase AWS-BT-12 controlled DRY-RUN (synthetic data — non-authoritative)\n")
    steps = [
        ("smoke_1m_nifty10", _syms("N10_", 10), "2024-05-01", 21, 100),
        ("y1_nifty50", _syms("N50_", 12), "2023-01-01", 252, 200),
        ("y5_nifty50", _syms("N50_", 12), "2019-01-01", 1260, 300),
        ("y5_nifty100", _syms("N100_", 18), "2019-01-01", 1260, 400),
        ("y12_highliq", _syms("HL_", 15), "2012-01-01", 3024, 500),
    ]
    runs = []
    for label, symbols, start, n, seed in steps:
        r = _run_universe(label, symbols, start, n, seed, registry, writer)
        runs.append(r)
        print(f"  [{label}] run_id={r['run_id']} trades={r['trades']} net_pnl={r['net_pnl']:,.0f} "
              f"PF={r['profit_factor']:.2f} gates_pass={r['gates']['overall_pass']}")

    print("\n  strategy ranking (synthetic intraday signal exercise)...")
    strat_rank = _strategy_ranking()

    # Symbol ranking from the long-horizon run.
    symbol_rank = runs[-1]["per_symbol_top"]

    # Exit-policy comparison (synthetic trades).
    e = pd.Timestamp("2020-06-01 10:00", tz=IST)
    spec = dict(symbol="HL_00", direction=Direction.BUY,
                entry_price=100.0, stop=95.0, quantity=10, entry_time=e)
    bars = [Candle("HL_00", "NSE", "EQ", "5m", e + pd.Timedelta(minutes=5 * i), 100 + i * 0.4,
                   100 + i * 0.5, 99.5, 100 + i * 0.4, 5000) for i in range(1, 20)]
    tee = compare_policies(spec, bars)
    tee_cmp = {p: {"realized_r": round(o.realized_r, 3), "capture": round(o.profit_capture_ratio, 3),
                   "giveback": round(o.giveback_ratio, 3), "mis_dependent": o.mis_dependent,
                   "final": o.final_reason} for p, o in tee.items()}

    # Walk-forward.
    def _wf_eval(params, fold, phase):
        edge = 4.0 if params["atr_stop_multiplier"] == 1.5 else 2.5
        f = 1.0 if phase == "train" else 0.75
        return {"expectancy": edge * f, "profit_factor": 1.4, "net_pnl": 1000 * edge * f}
    wf = run_walk_forward(start="2012-01-01", end="2024-01-01", spec=PRESETS["default"],
                          param_grid=[{"atr_stop_multiplier": 1.5}, {"atr_stop_multiplier": 2.0}],
                          evaluate=_wf_eval)
    wf_summary = {"folds": len(wf.folds), "stability": round(wf.stability_score, 3),
                  "unstable": wf.unstable, "overfit": wf.overfit_warning,
                  "eligibility": wf.eligibility,
                  "mean_oos_expectancy": round(wf.aggregate["mean_oos_expectancy"], 3),
                  "fold_count": len(fold_report(wf))}

    # Model dataset (synthetic signals from the long-horizon symbols).
    idx = pd.date_range("2012-01-01", periods=400, freq="D", tz=IST)
    price = pd.Series([100 + (i % 23) - 11 for i in range(len(idx))], index=idx)
    ds_sigs = []
    for i in range(120):
        t = idx[i]
        pnl = 80.0 if i % 3 else -60.0
        ds_sigs.append(SignalRecord(
            signal_id=f"bt12_s{i}", strategy="momentum", symbol="HL_00", timestamp=t,
            features={"rsi_14": 40 + (i % 30), "ema_ratio": 1.0 + (i % 5) * 0.01},
            entry_price=float(price.asof(t)), stop_price=float(price.asof(t)) - 1,
            target_price=float(price.asof(t)) + 2, exit_price=float(price.asof(t)) + (2 if pnl > 0 else -1),
            exit_reason="FINAL_TARGET" if pnl > 0 else "STOP_LOSS", net_pnl=pnl,
            mfe_r=2.0, mae_r=-0.6, realized_r=1.5 if pnl > 0 else -1.0, hit_tp_before_sl=pnl > 0,
            trust_level="HIGH"))
    ds_meta = DatasetMeta(dataset_id="ds_bt12", data_version=DATA_VERSION, code_version=CODE_VERSION,
                          strategy_version="momentum@2.0", exit_policy_version="tee@1.0",
                          embargo_minutes=2, source_run_ids=tuple(r["run_id"] for r in runs))
    ds = ModelDatasetBuilder().build_dataset(ds_sigs, {"HL_00": price}, ds_meta)
    ds_summary = {"dataset_id": ds.meta.dataset_id, "rows": ds.manifest["rows_total"],
                  "train": ds.manifest["rows_train"], "val": ds.manifest["rows_val"],
                  "test": ds.manifest["rows_test"], "authoritative": ds.manifest["authoritative"],
                  "features": ds.feature_columns}

    # GenAI report (stub, cited).
    from backtesting.genai import AnalysisContext, GenAIAnalyst, StubProvider
    analyst = GenAIAnalyst(StubProvider(response="Synthetic dry-run summary: momentum produced trades; "
                                        "gates evaluated; results non-authoritative pending real data."))
    big = runs[-1]
    ai = analyst.generate_report(AnalysisContext(
        run_id=big["run_id"], summary_text="AWS-BT-12 synthetic dry-run.",
        metrics={"net_pnl": big["net_pnl"], "profit_factor": big["profit_factor"]},
        sources=[{"id": f"run:{big['run_id']}/metrics.json", "ref": big["result_s3_path"]}]))
    genai_summary = {"model": ai.model, "template": ai.template_version, "cited": "## Sources" in ai.text}

    summary = {
        "phase": "AWS-BT-12", "mode": "DRY-RUN-SYNTHETIC", "authoritative": False,
        "code_version": CODE_VERSION, "data_version": DATA_VERSION,
        "runs": runs, "strategy_ranking": strat_rank, "symbol_ranking": symbol_rank,
        "tee_comparison": tee_cmp, "walk_forward": wf_summary, "model_dataset": ds_summary,
        "genai": genai_summary,
    }
    sp = out_dir / "_full_run_summary.json"
    sp.parent.mkdir(parents=True, exist_ok=True)
    sp.write_text(json.dumps(summary, indent=2, default=str))
    print(f"\n  wrote summary → {sp}")
    print("  DRY-RUN complete. NON-AUTHORITATIVE (synthetic). No live approved, no live enabled.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
