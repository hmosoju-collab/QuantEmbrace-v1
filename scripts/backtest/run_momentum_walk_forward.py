#!/usr/bin/env python3
"""Phase 15B — Walk-forward validation of the momentum strategy.

Validates MomentumStrategy (SMA crossover) on 47 NIFTY50 constituents over
2020–2024 using the default walk-forward preset (12-month IS, 3-month OOS,
3-month roll). Four short/long window combinations are explored.

For each fold:
  * Parameters are optimised on the TRAIN window only (IS phase).
  * The best-param set is validated on the immediately following OOS window.
  * OOS fold results are registered in the real DynamoDB qe-bt-runs table.

Eligibility verdict:
  ELIGIBLE_FOR_PAPER_PRIORITIZATION  — positive OOS edge, stable params, no overfit
  PAPER_OPTIMIZATION                 — positive but unstable / overfit → iterate
  REJECT                             — no positive OOS edge across folds

Backtest-only. Advisory. No broker. No live trading. No capital changes.
Advisory only — cannot promote. A human reviews all results.

Usage:
    python scripts/backtest/run_momentum_walk_forward.py
    python scripts/backtest/run_momentum_walk_forward.py --s3       # load from S3
    python scripts/backtest/run_momentum_walk_forward.py --dry-run  # preview folds
    python scripts/backtest/run_momentum_walk_forward.py --no-registry  # skip DynamoDB
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from datetime import date
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO / "services"))

import pandas as pd

from backtesting.metrics_engine import compute_metrics
from backtesting.replay_engine import DataFrameBarSource
from backtesting.run_registry import RunRegistry, RunSpec
from backtesting.strategy_adapter import get_adapter
from backtesting.walk_forward import PRESETS, fold_report, generate_folds, run_walk_forward
from strategy_engine.backtesting.backtester import Backtester, IndianCostModel

# ── configuration ────────────────────────────────────────────────────────────────

LOCAL_LAKE = str(_REPO / "backtest-data" / "lake" / "ohlcv")
S3_LAKE    = "s3://quantembrace-backtest-data/lake/ohlcv"

WF_START = date(2020, 1, 1)
WF_END   = date(2024, 12, 31)
PRESET   = "default"  # 12-month train / 3-month OOS / 3-month roll

COST_MODEL = IndianCostModel.delivery()

NIFTY50_SYMBOLS = [
    "ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK",
    "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL",
    "BPCL", "BRITANNIA", "CIPLA", "COALINDIA", "DIVISLAB",
    "DRREDDY", "EICHERMOT", "GRASIM", "HCLTECH", "HDFCBANK",
    "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK",
    "INDUSINDBK", "INFY", "ITC", "JSWSTEEL", "KOTAKBANK",
    "LT", "MARUTI", "NESTLEIND", "NTPC", "ONGC",
    "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHREECEM",
    "SUNPHARMA", "TATAMOTORS", "TATACONSUM", "TATASTEEL", "TCS",
    "TECHM", "TITAN",
]

# Parameter combinations to grid-search over TRAIN windows.
# short_window crossover with long_window; the SMA crossover IS the signal gate
# (min_confidence=0.0 for daily data — see Phase 13 findings).
PARAM_GRID = [
    {"short_window": 5,  "long_window": 20},   # fast — many signals
    {"short_window": 10, "long_window": 50},   # standard (Phase 13)
    {"short_window": 20, "long_window": 100},  # slow — fewer but bigger moves
    {"short_window": 10, "long_window": 100},  # hybrid — fast trigger, slow trend
]

RUNS_TABLE    = "qe-bt-runs"
CODE_VERSION  = "momentum-wf15b-v1"
DATA_VERSION  = "bhavcopy-nse-2020-2024-v1"
REGION        = "ap-south-1"


# ── data loading ─────────────────────────────────────────────────────────────────


def _load_local(lake_path: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        frames = []
        for year in range(2020, 2025):
            p = (
                Path(lake_path)
                / "market=NSE" / "segment=EQ"
                / f"symbol={sym}" / "interval=1d"
                / f"year={year}" / "part-0.parquet"
            )
            if p.exists():
                frames.append(pd.read_parquet(p))
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        df = _normalise(df, sym)
        data[sym] = df
    return data


def _load_s3(lake_path: str, symbols: list[str]) -> dict[str, pd.DataFrame]:
    import io

    import boto3
    s3     = boto3.client("s3", region_name=REGION)
    bucket = lake_path.split("/")[2]
    prefix = "/".join(lake_path.split("/")[3:])

    data: dict[str, pd.DataFrame] = {}
    for sym in symbols:
        frames = []
        for year in range(2020, 2025):
            key = f"{prefix}/market=NSE/segment=EQ/symbol={sym}/interval=1d/year={year}/part-0.parquet"
            try:
                obj = s3.get_object(Bucket=bucket, Key=key)
                frames.append(pd.read_parquet(io.BytesIO(obj["Body"].read())))
            except Exception:
                pass
        if not frames:
            continue
        df = pd.concat(frames, ignore_index=True)
        df = _normalise(df, sym)
        data[sym] = df
    return data


def _normalise(df: pd.DataFrame, sym: str) -> pd.DataFrame:
    ts = pd.to_datetime(df["timestamp"])
    if ts.dt.tz is None:
        ts = ts.dt.tz_localize("Asia/Kolkata")
    df = df.copy()
    df["timestamp"] = ts
    if "symbol"   not in df.columns: df["symbol"]   = sym
    if "market"   not in df.columns: df["market"]   = "NSE"
    if "segment"  not in df.columns: df["segment"]  = "EQ"
    if "interval" not in df.columns: df["interval"] = "1d"
    return df.sort_values("timestamp").reset_index(drop=True)


# ── backtesting helpers ──────────────────────────────────────────────────────────


async def _run_symbols_async(
    symbol_bars: list[tuple[str, list]],
    params: dict,
    cost_model: IndianCostModel,
) -> list:
    """Run Backtester over all symbols inside one event loop (re-entering is safe)."""
    adapter = get_adapter("momentum")
    all_trades = []
    for sym, bars in symbol_bars:
        strategy = adapter.build_strategy(
            [sym],
            short_window=params["short_window"],
            long_window=params["long_window"],
            min_confidence=0.0,
        )
        backtester = Backtester(
            strategy=strategy,
            slippage_bps=5.0,
            commission_pct=0.0,      # Zerodha equity delivery: Rs 0 brokerage
            indian_cost_model=cost_model,
        )
        result = await backtester.run(bars)
        all_trades.extend(result.trades)
    return all_trades


def _trades_to_df(trades: list) -> pd.DataFrame:
    """Convert list[TradeRecord] to the canonical trades DataFrame for metrics_engine."""
    if not trades:
        return pd.DataFrame(
            columns=["symbol", "net_pnl", "gross_pnl", "costs", "slippage",
                     "entry_time", "exit_time", "entry_price", "exit_price",
                     "quantity", "exit_reason"]
        )
    rows = []
    for t in trades:
        # t.pnl is "after commissions" (net). gross = net + commission costs.
        rows.append({
            "symbol":      t.symbol,
            "direction":   str(t.direction),
            "entry_time":  t.entry_time,
            "exit_time":   t.exit_time,
            "entry_price": t.entry_price,
            "exit_price":  t.exit_price,
            "quantity":    t.quantity,
            "net_pnl":     t.pnl,
            "costs":       t.commission,
            "gross_pnl":   t.pnl + t.commission,
            "slippage":    t.slippage,
            "exit_reason": t.exit_reason,
        })
    return pd.DataFrame(rows)


# ── evaluate callback factory ────────────────────────────────────────────────────


def _make_evaluate(lake_data: dict[str, pd.DataFrame], symbols: list[str], cost_model: IndianCostModel):
    """Return an ``evaluate(params, fold, phase) -> dict`` callback for run_walk_forward."""

    _IST = "Asia/Kolkata"

    def evaluate(params: dict, fold, phase: str) -> dict:
        raw_start = fold.train_start    if phase == "train"    else fold.validate_start
        raw_end   = fold.train_end      if phase == "train"    else fold.validate_end
        # Lake timestamps are tz-aware (IST); fold timestamps from generate_folds() are
        # tz-naive (just date boundaries). Localize to IST before comparing.
        start = pd.Timestamp(raw_start).tz_localize(_IST)
        end   = pd.Timestamp(raw_end).tz_localize(_IST)
        min_bars = params["long_window"] + 5

        symbol_bars: list[tuple[str, list]] = []
        for sym in symbols:
            df = lake_data.get(sym)
            if df is None:
                continue
            mask = (df["timestamp"] >= start) & (df["timestamp"] < end)
            window_df = df[mask]
            if len(window_df) < min_bars:
                continue
            candles = DataFrameBarSource.from_dataframe(window_df)._candles
            bars    = [c.to_bar() for c in candles]
            symbol_bars.append((sym, bars))

        if not symbol_bars:
            return {
                "expectancy": 0.0, "profit_factor": 0.0, "net_pnl": 0.0,
                "number_of_trades": 0, "win_rate": 0.0,
            }

        trades   = asyncio.run(_run_symbols_async(symbol_bars, params, cost_model))
        trades_df = _trades_to_df(trades)
        return compute_metrics(trades_df)

    return evaluate


# ── report writer ────────────────────────────────────────────────────────────────


def _write_report(result, rows: list[dict], spec, num_symbols: int, lake_path: str) -> Path:
    a = result.aggregate
    spec_obj = PRESETS[PRESET]
    report_dir = _REPO / "docs" / "backtesting"
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / "aws-phase15b-walk-forward-report.md"

    lines = [
        "# AWS Backtesting Lab — Phase 15B Report: Momentum Walk-Forward Validation",
        "",
        "**Status:** COMPLETE — awaiting human approval",
        f"**Date:** {date.today()}",
        "**Governance rule:** Every phase writes a report and stops. A human approves before the next phase begins.",
        "",
        "---",
        "",
        "## Scope",
        "",
        f"Walk-forward validation of the `momentum` strategy (SMA crossover) on {num_symbols} NIFTY50",
        f"constituents over 2020–2024 using the `{PRESET}` preset.",
        "",
        "| Parameter | Value |",
        "|---|---|",
        f"| Preset | `{PRESET}` — {spec_obj.train_months}-month IS / {spec_obj.validate_months}-month OOS / {spec_obj.roll_months}-month roll |",
        f"| Period | {WF_START} → {WF_END} |",
        f"| Symbols | {num_symbols} NIFTY50 (M&M / JIOFIN / ETERNAL excluded — post-2020 listing) |",
        f"| Folds | {a['folds']} IS→OOS folds |",
        "| Param grid | 4 (short/long window combinations) |",
        f"| Data source | {lake_path} |",
        f"| Data version | `{DATA_VERSION}` |",
        f"| Code version | `{CODE_VERSION}` |",
        f"| Cost model | `{COST_MODEL.cost_model_version}` (NSE equity delivery CNC) |",
        "| Slippage | 5 bps per leg |",
        "",
        "---",
        "",
        "## Parameter Grid",
        "",
        "| Combo | short\\_window | long\\_window | Description |",
        "|---|---|---|---|",
        "| A | 5 | 20 | Fast — many signals, captures short momentum bursts |",
        "| B | 10 | 50 | Standard (Phase 13 params) |",
        "| C | 20 | 100 | Slow — fewer signals, targets multi-month trends |",
        "| D | 10 | 100 | Hybrid — fast trigger, slow trend filter |",
        "",
        "---",
        "",
        "## Fold Table",
        "",
        "| Fold | Train window | OOS window | Best params | IS expectancy | OOS expectancy | OOS PF | Run ID |",
        "|---|---|---|---|---|---|---|---|",
    ]

    for r in rows:
        p = r["best_params"]
        params_str = f"sw={p['short_window']}, lw={p['long_window']}"
        lines.append(
            f"| {r['fold_id']} "
            f"| {r['train_start']} → {r['train_end']} "
            f"| {r['validate_start']} → {r['validate_end']} "
            f"| {params_str} "
            f"| ₹{r['is_objective']:.1f} "
            f"| ₹{r['oos_objective']:.1f} "
            f"| {r['oos_profit_factor']:.3f} "
            f"| `{r['run_id'] or 'n/a'}` |"
        )

    lines += [
        "",
        "---",
        "",
        "## Walk-Forward Aggregate",
        "",
        "| Metric | Value | Gate |",
        "|---|---|---|",
        f"| Folds | {a['folds']} | — |",
        f"| Mean IS expectancy | ₹{a['mean_is_objective']:.1f}/trade | — |",
        f"| Mean OOS expectancy | ₹{a['mean_oos_expectancy']:.1f}/trade | >₹0 → {'PASS' if a['gates']['expectancy_gt_0'] else 'FAIL'} |",
        f"| Mean OOS profit factor | {a['mean_oos_profit_factor']:.3f} | >1.2 → {'PASS' if a['gates']['profit_factor_gt_1_2'] else 'FAIL'} |",
        f"| Total OOS net P&L | ₹{a['total_oos_net_pnl']:,.0f} | >₹0 → {'PASS' if a['gates']['net_pnl_gt_0'] else 'FAIL'} |",
        ("| IS→OOS degradation | " + f"{a['is_oos_degradation']:.2f}" +
         " | >0.50 = not overfit → " + ("OK" if a["is_oos_degradation"] >= 0.50 else "OVERFIT WARNING") + " |"),
        ("| Win consistency | " + f"{a['win_consistency']:.2f}" +
         " | >0.50 = robust → " + ("OK" if a["win_consistency"] >= 0.50 else "INCONSISTENT") + " |"),
        ("| Parameter stability | " + f"{result.stability_score:.2f}" +
         " | >0.70 = stable → " + ("OK" if not result.unstable else "UNSTABLE") + " |"),
        "",
        "---",
        "",
        "## Parameter Robustness",
        "",
    ]

    if result.parameter_robustness:
        lines.append("How many folds selected each param value:")
        lines.append("")
        for param, counts in result.parameter_robustness.items():
            lines.append(f"**{param}:**")
            for val, cnt in sorted(counts.items(), key=lambda x: -x[1]):
                lines.append(f"  - `{val}` → {cnt}/{a['folds']} folds")
        lines.append("")

    if result.notes:
        lines += ["---", "", "## Warnings", ""]
        for n in result.notes:
            lines.append(f"- {n}")
        lines.append("")

    lines += [
        "---",
        "",
        "## Eligibility Verdict",
        "",
        f"```",
        f"{result.eligibility}",
        f"```",
        "",
    ]

    if result.eligibility == "ELIGIBLE_FOR_PAPER_PRIORITIZATION":
        lines += [
            "The momentum strategy passes all OOS gates, is stable across folds, and shows",
            "no significant overfitting. This is an **advisory** recommendation to prioritise",
            "this strategy in paper session analysis.",
            "",
        ]
    elif result.eligibility == "PAPER_OPTIMIZATION":
        lines += [
            "OOS gates pass (positive expectancy / profit factor) but instability or",
            "overfitting indicators are present. Continue paper optimization before",
            "considering promotion.",
            "",
        ]
    else:
        lines += [
            "OOS gates do not pass. Momentum in this configuration does not show robust",
            "edge across the validation windows. Further parameter exploration or a different",
            "strategy is warranted.",
            "",
        ]

    lines += [
        "---",
        "",
        "## Advisory Conclusions",
        "",
        "> **Backtesting can recommend. Backtesting cannot promote. A human approves all production changes.**",
        "",
        "Walk-forward validation gives a more robust estimate of out-of-sample edge than",
        "a single held-out period. However:",
        "",
        "- These results are advisory and non-authoritative for promotion.",
        "- Live trading remains BLOCKED — paper session gates (≥5 consecutive passing sessions)",
        "  have not been cleared.",
        "- Shard-based capital resets still apply within each symbol window, so Sharpe and",
        "  annualised return remain unreliable.",
        "- The walk-forward IS→OOS gap tests for parameter overfitting, not concept-level edge.",
        "",
        "---",
        "",
        "## Phase 16 Options (operator selects)",
        "",
        "**A. Intraday data acquisition** — identify a vendor (TrueData / GlobalDataFeeds /",
        "  NSE historical API) to enable the remaining 5 strategies on 1m/5m/15m data.",
        "",
        "**B. Portfolio-level Sharpe fix** — rerun walk-forward with `partition_by='symbol'`",
        "  (all years in one shard per symbol) for a reliable equity curve and Sharpe.",
        "",
        "**C. ORB / VWAP on a subset of intraday data** — if any intraday data is available,",
        "  run a single-strategy probe on 1m Bhavcopy data for ORB.",
        "",
        "**D. GenAI analysis** (Phase 10) — run Bedrock analysis over the walk-forward",
        "  artifacts to generate a narrative summary and strategy improvement suggestions.",
        "",
        "---",
        "",
        "## Approval Required",
        "",
        "Per governance: **a human must approve this report.**",
        "",
        "Checklist for approver:",
        "- [ ] Walk-forward setup reviewed (preset, param grid, date range, symbols)",
        "- [ ] Fold table reviewed (IS→OOS expectancy pairs, param selection pattern)",
        "- [ ] Aggregate metrics reviewed (degradation, win consistency, stability)",
        "- [ ] Eligibility verdict understood and advisory-only nature confirmed",
        "- [ ] No production changes will be made based solely on this report",
        "- [ ] Next phase selected from A / B / C / D above",
    ]

    path.write_text("\n".join(lines) + "\n")
    return path


# ── main ─────────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Phase 15B — momentum walk-forward validation")
    ap.add_argument("--s3", action="store_true", help="Load from S3 lake instead of local")
    ap.add_argument("--no-registry", action="store_true", help="Skip DynamoDB run registration")
    ap.add_argument("--dry-run", action="store_true", help="Preview fold schedule without running")
    ap.add_argument("--preset", choices=list(PRESETS), default=PRESET,
                    help=f"Walk-forward preset (default: {PRESET}). "
                         "Use 'medium' (24/6/6) for Phase 15C to give lw=100 a fair OOS window.")
    ap.add_argument("--lw-max", type=int, default=None,
                    help="Exclude param combos with long_window > LW_MAX (e.g. --lw-max 50 for Phase 15C)")
    args = ap.parse_args()

    lake_path = S3_LAKE if args.s3 else LOCAL_LAKE
    spec      = PRESETS[args.preset]

    # Allow Phase 15C re-run: filter out lw > lw_max if requested
    param_grid = PARAM_GRID
    if args.lw_max is not None:
        param_grid = [p for p in PARAM_GRID if p["long_window"] <= args.lw_max]
        if not param_grid:
            print(f"ERROR: --lw-max {args.lw_max} excluded all param combinations.")
            return 1

    print("=" * 72)
    print("QuantEmbrace — Momentum Walk-Forward Validation (Phase 15B)")
    print("Backtest-only. Advisory. No broker. No live trading.")
    print("=" * 72)
    print(f"  Lake      : {lake_path}")
    print(f"  Period    : {WF_START} → {WF_END}")
    print(f"  Preset    : {args.preset}  (train={spec.train_months}m / OOS={spec.validate_months}m / roll={spec.roll_months}m)")
    print(f"  Symbols   : {len(NIFTY50_SYMBOLS)} NIFTY50")
    print(f"  Param grid: {len(param_grid)} combinations  (short/long windows)")
    print(f"  Objective : expectancy")
    print(f"  Costs     : {COST_MODEL.cost_model_version}")
    print()

    if args.dry_run:
        folds = generate_folds(WF_START, WF_END, spec)
        print(f"  DRY RUN — {len(folds)} folds would be generated")
        print()
        print(f"  {'Fold':>4}  {'Train start':>12}  {'Train end':>12}  {'OOS start':>12}  {'OOS end':>12}")
        print("  " + "-" * 62)
        for f in folds:
            print(
                f"  {f.fold_id:>4}  {f.train_start.date()!s:>12}  "
                f"{f.train_end.date()!s:>12}  "
                f"{f.validate_start.date()!s:>12}  "
                f"{f.validate_end.date()!s:>12}"
            )
        total_bt = len(folds) * len(param_grid) * len(NIFTY50_SYMBOLS)
        print()
        pg_str = " | ".join(f"sw={p['short_window']},lw={p['long_window']}" for p in param_grid)
        print(f"  Param grid: {len(param_grid)} combos  [{pg_str}]")
        print(f"  Total backtest calls: {len(folds)} folds × {len(param_grid)} params × {len(NIFTY50_SYMBOLS)} symbols = {total_bt:,}")
        print("  (Plus 1 OOS call per fold for the best params.)")
        return 0

    # ── load data ──────────────────────────────────────────────────────────────
    print("  Loading lake data into memory...")
    t0 = time.monotonic()
    lake_data = (_load_s3 if args.s3 else _load_local)(lake_path, NIFTY50_SYMBOLS)
    loaded    = len(lake_data)
    print(f"  Loaded {loaded} of {len(NIFTY50_SYMBOLS)} symbols in {time.monotonic()-t0:.1f}s")

    if loaded == 0:
        print("ERROR: No symbols loaded — check lake path and ensure data is available.")
        return 1

    symbols = list(lake_data.keys())

    # ── registry ───────────────────────────────────────────────────────────────
    registry      = None
    run_spec_fact = None

    if not args.no_registry:
        try:
            import boto3
            dynamodb = boto3.resource("dynamodb", region_name=REGION)
            registry = RunRegistry(dynamodb.Table(RUNS_TABLE))
            print(f"  Registry  : DynamoDB {RUNS_TABLE} ({REGION})")

            def run_spec_fact(fold, params: dict) -> RunSpec:
                return RunSpec(
                    strategy="momentum",
                    symbols=symbols,
                    timeframe="1d",
                    start_date=fold.validate_start.date(),
                    end_date=fold.validate_end.date(),
                    config_s3_path=(
                        f"s3://quantembrace-backtest-results/configs/"
                        f"wf15b-fold{fold.fold_id}-sw{params['short_window']}-lw{params['long_window']}.json"
                    ),
                    data_version=DATA_VERSION,
                    code_version=CODE_VERSION,
                    cost_model_version=COST_MODEL.cost_model_version,
                    exit_policy_version="v1",
                )

        except Exception as exc:
            print(f"  WARN: could not connect to DynamoDB registry: {exc}")
            print("        Run with --no-registry to suppress. OOS runs will not be registered.")

    print()
    folds = generate_folds(WF_START, WF_END, spec)
    total_bt = len(folds) * len(param_grid) * len(symbols) + len(folds) * len(symbols)
    print(f"  Running {len(folds)} folds × {len(param_grid)} param combos × {len(symbols)} symbols...")
    print(f"  Total backtest calls: ≈{total_bt:,}  (incl. OOS calls)")
    print()

    evaluate = _make_evaluate(lake_data, symbols, COST_MODEL)

    t1 = time.monotonic()
    result = run_walk_forward(
        start=WF_START,
        end=WF_END,
        spec=spec,
        param_grid=param_grid,
        evaluate=evaluate,
        objective="expectancy",
        registry=registry,
        run_spec_factory=run_spec_fact,
    )
    elapsed = time.monotonic() - t1

    # ── fold table ─────────────────────────────────────────────────────────────
    rows = fold_report(result)
    hdr  = f"{'Fold':>4}  {'Train':>11} → {'':11}  {'OOS':>11} → {'':11}  {'Params':>18}  {'IS exp':>8}  {'OOS exp':>8}  {'OOS PF':>8}"
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        p   = r["best_params"]
        pst = f"sw={p['short_window']:2d}, lw={p['long_window']:3d}"
        print(
            f"{r['fold_id']:>4}  {r['train_start']:>11} → {r['train_end']:>11}  "
            f"{r['validate_start']:>11} → {r['validate_end']:>11}  "
            f"{pst:>18}  {r['is_objective']:>8.1f}  {r['oos_objective']:>8.1f}  {r['oos_profit_factor']:>8.3f}"
        )
    print()

    # ── aggregate ──────────────────────────────────────────────────────────────
    a = result.aggregate
    gates = a["gates"]
    print("=" * 72)
    print("WALK-FORWARD AGGREGATE")
    print(f"  Folds              : {a['folds']}")
    print(f"  Mean IS expectancy : ₹{a['mean_is_objective']:>10,.1f}/trade")
    print(f"  Mean OOS expectancy: ₹{a['mean_oos_expectancy']:>10,.1f}/trade"
          f"  (gate >₹0 → {'PASS' if gates['expectancy_gt_0'] else 'FAIL'})")
    print(f"  Mean OOS profit fac: {a['mean_oos_profit_factor']:>13.3f}"
          f"  (gate >1.2 → {'PASS' if gates['profit_factor_gt_1_2'] else 'FAIL'})")
    print(f"  Total OOS net P&L  : ₹{a['total_oos_net_pnl']:>10,.0f}"
          f"  (gate >₹0 → {'PASS' if gates['net_pnl_gt_0'] else 'FAIL'})")
    print(f"  IS→OOS degradation : {a['is_oos_degradation']:>13.2f}"
          f"  (>0.50 = not overfit → {'OK' if a['is_oos_degradation'] >= 0.50 else 'OVERFIT WARNING'})")
    print(f"  Win consistency    : {a['win_consistency']:>13.2f}"
          f"  (>0.50 = robust → {'OK' if a['win_consistency'] >= 0.50 else 'INCONSISTENT'})")
    print(f"  Param stability    : {result.stability_score:>13.2f}"
          f"  (>0.70 = stable → {'OK' if not result.unstable else 'UNSTABLE'})")
    print()

    if result.parameter_robustness:
        print("  PARAMETER ROBUSTNESS (folds selecting each value):")
        for param, counts in result.parameter_robustness.items():
            freq = " | ".join(f"{v}={c}" for v, c in sorted(counts.items(), key=lambda x: -x[1]))
            print(f"    {param}: {freq}")
        print()

    if result.notes:
        print("  NOTES:")
        for n in result.notes:
            print(f"    • {n}")
        print()

    print(f"  ► ELIGIBILITY VERDICT: {result.eligibility}")
    print(f"  ► Completed in {elapsed:.1f}s")
    print("=" * 72)
    print()

    # ── write report ───────────────────────────────────────────────────────────
    report_path = _write_report(result, rows, spec, loaded, lake_path)
    print(f"  Report   : {report_path}")
    print()
    print("Advisory only. Backtesting can recommend. It cannot promote.")
    print("A human approves all production changes.")
    print("Live trading remains BLOCKED until ≥5 consecutive valid paper sessions")
    print("pass all strategy performance gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
