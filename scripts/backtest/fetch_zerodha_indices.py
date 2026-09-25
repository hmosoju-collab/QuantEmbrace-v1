#!/usr/bin/env python3
"""
QuantEmbrace — Zerodha Kite index / India-VIX historical fetcher (vol-track O-1).

Pulls daily (and optionally intraday) history for the *underlying* inputs the
volatility-premium screen needs — NIFTY 50 / NIFTY BANK spot and **INDIA VIX** —
into the same Parquet lake the backtesting lab already reads, under
``segment=INDICES`` (vs ``segment=EQ`` for the equity backbone).

WHY A SEPARATE THIN FETCHER
---------------------------
The equity fetcher (``fetch_zerodha_intraday.py``) is the well-tested EQ
backbone; we do NOT thread index/segment branches through it and risk that path.
Instead we **reuse its pure plumbing** (date chunking, IST normalisation, rate
limiter, Kite client builder, interval map, Parquet schema) and add only the
index-specific bits: known index instrument tokens, ``segment=INDICES`` framing,
and lenient volume handling (INDIA VIX has no volume).

WHY THESE INPUTS (vol-track O-1, the FREE screen)
-------------------------------------------------
Kite ``historical_data`` needs an ``instrument_token``; expired weekly-option
tokens are purged from the instrument master, so 3–5 yr of option *chains* is NOT
cheaply available from Kite — that needs a paid vendor (O-2). But the *underlying*
vol-risk-premium phenomenon can be screened for FREE from:

  • INDIA VIX (implied vol, annualised %)         → token 264969
  • NIFTY 50 spot (to compute realised vol)        → token 256265
  • NIFTY BANK spot (secondary)                    → token 260105

``run_vol_premium_study.py`` consumes these. If the VRP screen there does not
clear its pre-registered bar, we SHELVE before paying for option-chain history.

RUN THIS LOCALLY
----------------
Like the equity fetcher and ``download_bhavcopy.py``, this needs a live same-day
Kite session — the sandbox has no egress to api.kite.trade. Refresh first:

    python scripts/zerodha_login.py            # writes today's token (expires 07:30 IST)
    export ZERODHA_API_KEY=...                 # or rely on .env
    export ZERODHA_ACCESS_TOKEN=...            # printed by zerodha_login.py

Then fetch (daily, ~5 yr — the core VRP inputs):

    python scripts/backtest/fetch_zerodha_indices.py \
        --indices niftyvix --intervals 1d --start 2020-01-01 --end 2025-12-31

Idempotent — re-run to resume (skips year partitions already written, unless
``--force``). Backtest-only. No orders, no live/paper trading, no capital changes.

Install deps first:
    pip install kiteconnect pandas pyarrow python-dotenv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

# ── reuse the equity fetcher's well-tested pure plumbing (no rewrite) ──────────
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
from fetch_zerodha_intraday import (  # noqa: E402
    _date_chunks, _to_ist, _rate_sleep, _build_kite, _INTERVALS, PARQUET_SCHEMA,
)

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_IST = ZoneInfo("Asia/Kolkata")

SOURCE_NAME = "zerodha_kite"
TRUST_LEVEL = "HIGH"
LICENSE = "kite-connect-personal-use"

# ── known, stable Kite index instrument tokens (lake symbol → (token, kite name)) ──
# These are constants in Kite's instrument master; we hardcode them so we don't
# depend on a fragile name match in a multi-megabyte instruments() dump.
INDEX_TOKENS: dict[str, tuple[int, str]] = {
    "NIFTY50":   (256265, "NIFTY 50"),
    "BANKNIFTY": (260105, "NIFTY BANK"),
    "INDIAVIX":  (264969, "INDIA VIX"),
    "FINNIFTY":  (257801, "NIFTY FIN SERVICE"),
}
# Named bundles for convenience.
INDEX_BUNDLES: dict[str, list[str]] = {
    "niftyvix":  ["NIFTY50", "INDIAVIX"],                 # the minimal VRP screen inputs
    "all":       ["NIFTY50", "BANKNIFTY", "INDIAVIX", "FINNIFTY"],
}


def _lake_dir(base: Path, symbol: str, interval: str, year: int) -> Path:
    return (
        base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES"
        / f"symbol={symbol}" / f"interval={interval}" / f"year={year}"
    )


def _manifest_path(base: Path) -> Path:
    return base / "raw" / "zerodha_indices" / "_manifest.json"


def _candles_to_frame(symbol: str, interval: str, candles: list[dict]) -> pd.DataFrame:
    """Index/VIX candles → canonical frame (segment=INDICES, volume may be 0)."""
    rows = []
    for c in candles:
        ts = _to_ist(c.get("date"))
        if ts is None:
            continue
        o, h, l, cl = (
            float(c.get("open", 0)), float(c.get("high", 0)),
            float(c.get("low", 0)), float(c.get("close", 0)),
        )
        # Index/VIX sanity: ordered OHLC and positive close. No volume requirement
        # (INDIA VIX and index levels carry no traded volume).
        if not (l <= o <= h and l <= cl <= h and cl > 0):
            continue
        rows.append({
            "timestamp": ts,
            "symbol": symbol,
            "market": "NSE",
            "segment": "INDICES",
            "interval": interval,
            "open": o, "high": h, "low": l, "close": cl,
            "volume": int(c.get("volume", 0) or 0),
            "source": SOURCE_NAME,
            "trust_level": TRUST_LEVEL,
        })
    return pd.DataFrame(rows)


def _write_year_partitions(base: Path, symbol: str, interval: str, df: pd.DataFrame,
                           force: bool, verbose: bool) -> dict[int, int]:
    written: dict[int, int] = {}
    if df.empty:
        return written
    df = df.sort_values("timestamp").drop_duplicates(subset=["timestamp"])
    df["_year"] = df["timestamp"].dt.year
    for year, grp in df.groupby("_year"):
        out_dir = _lake_dir(base, symbol, interval, int(year))
        out_path = out_dir / "part-0.parquet"
        if out_path.exists() and not force:
            if verbose:
                print(f"    skip {symbol}/{interval}/{year} (exists)")
            continue
        grp = grp.drop(columns=["_year"])
        grp = grp[[f.name for f in PARQUET_SCHEMA]]
        table = pa.Table.from_pandas(grp, schema=PARQUET_SCHEMA, preserve_index=False)
        out_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, out_path, compression="snappy")
        written[int(year)] = len(grp)
        if verbose:
            print(f"    wrote {symbol}/{interval}/{year}: {len(grp)} bars")
    return written


def _update_manifest(base: Path, entries: list[dict]) -> None:
    mpath = _manifest_path(base)
    mpath.parent.mkdir(parents=True, exist_ok=True)
    existing = []
    if mpath.exists():
        try:
            existing = json.loads(mpath.read_text()).get("entries", [])
        except json.JSONDecodeError:
            existing = []
    existing.extend(entries)
    mpath.write_text(json.dumps({
        "source": SOURCE_NAME, "trust_level": TRUST_LEVEL, "license": LICENSE,
        "note": ("Zerodha Kite historical_data — NSE indices + INDIA VIX for the "
                 "vol-premium screen (O-1). Personal-use license: do not "
                 "redistribute raw data."),
        "entries": existing,
    }, indent=2))


def fetch(kite: Any, symbols: list[str], intervals: list[str], start: date, end: date,
          base: Path, force: bool, verbose: bool) -> int:
    bad_sym = [s for s in symbols if s not in INDEX_TOKENS]
    if bad_sym:
        print(f"ERROR: unknown index symbols {bad_sym}. Choose from {list(INDEX_TOKENS)}.",
              file=sys.stderr)
        return 1

    total_bars = 0
    total_requests = 0
    manifest_entries: list[dict] = []

    for idx, symbol in enumerate(symbols, start=1):
        token, _kite_name = INDEX_TOKENS[symbol]
        for interval in intervals:
            kite_interval, max_days = _INTERVALS[interval]
            frames: list[pd.DataFrame] = []
            for (c_start, c_end) in _date_chunks(start, end, max_days):
                call_start = time.monotonic()
                try:
                    candles = kite.historical_data(
                        token, c_start, c_end, kite_interval, continuous=False,
                    )
                    total_requests += 1
                except Exception as exc:
                    print(f"  [{idx}/{len(symbols)}] ERROR {symbol}/{interval} "
                          f"{c_start}→{c_end}: {exc}", file=sys.stderr)
                    _rate_sleep(call_start)
                    continue
                if candles:
                    frames.append(_candles_to_frame(symbol, interval, candles))
                _rate_sleep(call_start)

            if not frames:
                print(f"  [{idx}/{len(symbols)}] {symbol}/{interval}: no data")
                continue
            df = pd.concat(frames, ignore_index=True)
            written = _write_year_partitions(base, symbol, interval, df, force, verbose)
            bars = sum(written.values())
            total_bars += bars
            if written:
                manifest_entries.append({
                    "symbol": symbol, "interval": interval, "instrument_token": token,
                    "years": {str(y): n for y, n in sorted(written.items())},
                    "bars": bars,
                    "sha256": hashlib.sha256(
                        pd.util.hash_pandas_object(df, index=False).values.tobytes()
                    ).hexdigest(),
                    "fetched_at": datetime.now(tz=_IST).isoformat(),
                })
            print(f"  [{idx}/{len(symbols)}] {symbol}/{interval}: {bars} bars "
                  f"across {len(written)} year(s)")

    if manifest_entries:
        _update_manifest(base, manifest_entries)

    print()
    print(f"Fetch complete: {total_bars:,} bars, {total_requests} API calls, "
          f"{len(manifest_entries)} symbol/interval partitions written.")
    print(f"Lake: {base / 'lake' / 'ohlcv'} (segment=INDICES)")
    print("Backtest-only. Advisory. No live trading. No orders.")
    return 0


# ── self-test (no network) ────────────────────────────────────────────────────


class _StubKite:
    """In-memory Kite stand-in: synthetic daily index + VIX candles."""

    def historical_data(self, token, from_date, to_date, interval, continuous=False):
        out = []
        d = from_date if isinstance(from_date, date) else date.fromisoformat(str(from_date)[:10])
        last = to_date if isinstance(to_date, date) else date.fromisoformat(str(to_date)[:10])
        is_vix = token == 264969
        lvl = 14.0 if is_vix else 18000.0
        while d <= last:
            if d.weekday() < 5:
                lvl += 0.05 if is_vix else 5.0
                o = lvl
                h = lvl * 1.01
                l = lvl * 0.99
                cl = lvl * 1.002
                out.append({
                    "date": datetime(d.year, d.month, d.day, 15, 30, tzinfo=_IST),
                    "open": round(o, 2), "high": round(h, 2),
                    "low": round(l, 2), "close": round(cl, 2),
                    "volume": 0 if is_vix else 100000,
                })
            d += timedelta(days=1)
        return out


def _self_test() -> int:
    import tempfile
    print("SELF-TEST: fetching synthetic index + VIX daily (no network)...")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        rc = fetch(_StubKite(), ["NIFTY50", "INDIAVIX"], ["1d"],
                   date(2024, 1, 1), date(2024, 2, 29), base, force=False, verbose=True)
        assert rc == 0
        p = (base / "lake" / "ohlcv" / "market=NSE" / "segment=INDICES"
             / "symbol=INDIAVIX" / "interval=1d" / "year=2024" / "part-0.parquet")
        assert p.exists(), f"expected VIX partition missing: {p}"
        df = pd.read_parquet(p)
        assert df["timestamp"].dt.tz is not None
        assert (df["segment"] == "INDICES").all()
        assert (df["volume"] == 0).all(), "VIX must carry zero volume"
        assert (df["close"] > 0).all()
        man = json.loads(_manifest_path(base).read_text())
        assert man["source"] == SOURCE_NAME and len(man["entries"]) >= 2
        print(f"  OK — {len(df)} VIX daily bars, segment=INDICES, manifest "
              f"{len(man['entries'])} entries.")
    print("SELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Zerodha Kite index / India-VIX history into the lake")
    ap.add_argument("--indices", choices=list(INDEX_BUNDLES), default="niftyvix",
                    help="Predefined index bundle (default: niftyvix = NIFTY50 + INDIA VIX)")
    ap.add_argument("--symbols", help="Comma-separated index symbols (overrides --indices): "
                    + ",".join(INDEX_TOKENS))
    ap.add_argument("--intervals", default="1d",
                    help="Comma-separated intervals (default: 1d). Intraday from "
                    + ",".join(k for k in _INTERVALS if k != "1d"))
    ap.add_argument("--start", default="2020-01-01", help="Start date YYYY-MM-DD")
    ap.add_argument("--end", default=date.today().isoformat(), help="End date YYYY-MM-DD")
    ap.add_argument("--base", default=str(_DEFAULT_BASE), help="Lake base directory")
    ap.add_argument("--force", action="store_true", help="Overwrite existing year partitions")
    ap.add_argument("--dry-run", action="store_true", help="Show the fetch plan without calling Kite")
    ap.add_argument("--verbose", action="store_true", help="Per-partition logging")
    ap.add_argument("--self-test", action="store_true", help="Run offline self-test (no network/creds)")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    symbols = (
        [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        if args.symbols else INDEX_BUNDLES[args.indices]
    )
    intervals = [s.strip() for s in args.intervals.split(",") if s.strip()]
    bad = [i for i in intervals if i not in _INTERVALS]
    if bad:
        print(f"ERROR: unsupported intervals {bad}. Choose from {list(_INTERVALS)}.", file=sys.stderr)
        return 1

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    base = Path(args.base)

    print("=" * 72)
    print("QuantEmbrace — Zerodha Kite Index / India-VIX Fetch (vol-track O-1)")
    print("Backtest-only. Free underlying inputs for the VRP screen. No live trading.")
    print("=" * 72)
    print(f"  Indices  : {symbols}")
    print(f"  Intervals: {intervals}")
    print(f"  Period   : {start} → {end}")
    print(f"  Lake     : {base / 'lake' / 'ohlcv'} (segment=INDICES)")
    print(f"  Source   : {SOURCE_NAME} (trust={TRUST_LEVEL}, license={LICENSE})")
    print()

    if args.dry_run:
        print("  DRY RUN — fetch plan:")
        total_reqs = 0
        for interval in intervals:
            _, max_days = _INTERVALS[interval]
            n_chunks = len(_date_chunks(start, end, max_days))
            total_reqs += n_chunks * len(symbols)
            print(f"    {interval}: {n_chunks} chunk(s)/symbol × {len(symbols)} symbols "
                  f"= {n_chunks * len(symbols)} requests")
        print(f"  Total ≈ {total_reqs} requests, ~{total_reqs / 3.0:.1f}s at 3 req/sec.")
        return 0

    kite = _build_kite()
    return fetch(kite, symbols, intervals, start, end, base, args.force, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
