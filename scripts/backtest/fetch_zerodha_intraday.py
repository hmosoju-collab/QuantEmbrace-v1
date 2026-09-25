#!/usr/bin/env python3
"""
QuantEmbrace — Zerodha Kite intraday historical fetcher (backtesting lab Phase B).

Pulls 1m / 5m / 15m OHLCV history for a liquid NSE universe from the Zerodha
Kite Connect ``historical_data`` endpoint and normalises it into the local
Parquet lake the backtesting lab already reads (same layout as the daily
Bhavcopy backbone, just ``interval=1m|5m|15m`` instead of ``interval=1d``).

WHY THIS SOURCE
---------------
Per ``docs/backtesting/intraday-data-procurement-memo.md`` and the data-lake
contract §1, Zerodha Kite is the pragmatic "limited intraday" tier: free with a
base Connect subscription, exchange-validated candles, ~3 years of 1-minute
depth for liquid names. It is HIGH trust for *provenance* but LIMITED depth —
use it to test for intraday edge cheaply, NOT as a 15-year authoritative
backbone. A positive result here warrants procuring deeper licensed vendor data
(TrueData / GlobalDataFeeds) before any further validation.

RUN THIS LOCALLY
----------------
Like ``download_bhavcopy.py``, this is an operator-run script — it needs a live,
same-day Kite session. The sandbox has neither network egress to api.kite.trade
nor a fresh token. Refresh the token first:

    python scripts/zerodha_login.py            # writes today's token (expires 07:30 IST)
    export ZERODHA_API_KEY=...                 # or rely on .env
    export ZERODHA_ACCESS_TOKEN=...            # printed by zerodha_login.py

Then fetch (NIFTY-50 liquid universe, 1m/5m/15m, ~3 yr):

    python scripts/backtest/fetch_zerodha_intraday.py \
        --universe nifty50 --intervals 1m,5m,15m \
        --start 2022-01-01 --end 2024-12-31

Idempotent — re-run to resume (skips symbol/interval/year partitions already
written, unless ``--force``).

Backtest-only. Reads historical market data via the 3 req/sec historical budget.
No orders, no live/paper trading, no capital changes.

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

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"
_IST = ZoneInfo("Asia/Kolkata")

# ── Source provenance (data-lake contract) ───────────────────────────────────
SOURCE_NAME = "zerodha_kite"   # classified HIGH by s3_data_catalog.classify_source_trust
TRUST_LEVEL = "HIGH"           # provenance — exchange-validated broker feed
LICENSE     = "kite-connect-personal-use"  # personal ToS; do NOT redistribute raw data

# ── Kite historical interval map: canonical lake name → (kite name, max days/request) ──
# Kite caps the date span per historical_data request by interval granularity.
_INTERVALS: dict[str, tuple[str, int]] = {
    "1m":  ("minute",   60),
    "3m":  ("3minute",  90),
    "5m":  ("5minute",  90),
    "10m": ("10minute", 90),
    "15m": ("15minute", 180),
    "30m": ("30minute", 180),
    "60m": ("60minute", 365),
    "1d": ("day",      2000),   # daily candles (used by the indices/VIX vol-track fetcher)
}

# Rate limit: separate 3 req/sec historical data budget (NOT the order budget).
_HIST_INTERVAL_SECONDS = 1.0 / 3.0   # ~333ms between calls

# ── Canonical intraday Parquet schema (compatible with data_loader.load_candles
#    and replay_engine.DataFrameBarSource.from_dataframe) ───────────────────────
PARQUET_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("us", tz="Asia/Kolkata")),
    ("symbol", pa.string()),
    ("market", pa.string()),
    ("segment", pa.string()),
    ("interval", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.int64()),
    ("source", pa.string()),
    ("trust_level", pa.string()),
])

# ── Default universes (plain NSE tradingsymbols) ─────────────────────────────
# NIFTY-50 minus 3 post-2020 listings handled elsewhere; intraday depth is best
# on liquid large-caps, so the full NIFTY-50 is the sensible default.
NIFTY50 = [
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
# A small liquid subset for cheap first probes (fast, low API budget).
LIQUID10 = [
    "RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS",
    "SBIN", "AXISBANK", "KOTAKBANK", "BHARTIARTL", "LT",
]
UNIVERSES = {"nifty50": NIFTY50, "liquid10": LIQUID10}


# ── lake paths ───────────────────────────────────────────────────────────────


def _lake_dir(base: Path, symbol: str, interval: str, year: int) -> Path:
    sym = symbol.replace("/", "_").replace("&", "_").strip()
    return (
        base / "lake" / "ohlcv" / "market=NSE" / "segment=EQ"
        / f"symbol={sym}" / f"interval={interval}" / f"year={year}"
    )


def _manifest_path(base: Path) -> Path:
    return base / "raw" / "zerodha_intraday" / "_manifest.json"


# ── chunking ─────────────────────────────────────────────────────────────────


def _date_chunks(start: date, end: date, max_days: int) -> list[tuple[date, date]]:
    """Split [start, end] into <= max_days windows (Kite per-request cap)."""
    chunks: list[tuple[date, date]] = []
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=max_days - 1), end)
        chunks.append((cur, chunk_end))
        cur = chunk_end + timedelta(days=1)
    return chunks


def _to_ist(raw_dt: Any) -> Optional[pd.Timestamp]:
    """Normalise a Kite candle 'date' to a tz-aware IST pandas Timestamp."""
    if raw_dt is None:
        return None
    ts = pd.Timestamp(raw_dt)
    if ts.tz is None:
        ts = ts.tz_localize(_IST)
    else:
        ts = ts.tz_convert(_IST)
    return ts


def _candles_to_frame(symbol: str, interval: str, candles: list[dict]) -> pd.DataFrame:
    rows = []
    for c in candles:
        ts = _to_ist(c.get("date"))
        if ts is None:
            continue
        o, h, l, cl = (
            float(c.get("open", 0)), float(c.get("high", 0)),
            float(c.get("low", 0)), float(c.get("close", 0)),
        )
        # Basic OHLC sanity — drop malformed bars (no silent bad data in the lake)
        if not (l <= o <= h and l <= cl <= h and o > 0 and cl > 0):
            continue
        rows.append({
            "timestamp": ts,
            "symbol": symbol,
            "market": "NSE",
            "segment": "EQ",
            "interval": interval,
            "open": o, "high": h, "low": l, "close": cl,
            "volume": int(c.get("volume", 0) or 0),
            "source": SOURCE_NAME,
            "trust_level": TRUST_LEVEL,
        })
    return pd.DataFrame(rows)


def _write_year_partitions(base: Path, symbol: str, interval: str, df: pd.DataFrame,
                           force: bool, verbose: bool) -> dict[int, int]:
    """Write one Parquet file per IST year for (symbol, interval). Returns {year: rows}."""
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
        # Coerce to canonical schema column order
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
        "source": SOURCE_NAME,
        "trust_level": TRUST_LEVEL,
        "license": LICENSE,
        "note": (
            "Zerodha Kite historical_data — limited-depth intraday for edge "
            "exploration. NOT a 15-yr authoritative backbone. Personal-use "
            "license: do not redistribute raw data."
        ),
        "entries": existing,
    }, indent=2))


# ── token resolution ─────────────────────────────────────────────────────────


def _build_kite() -> Any:
    """Construct an authenticated KiteConnect client from env credentials."""
    import os

    try:
        from kiteconnect import KiteConnect
    except ImportError:
        print("ERROR: kiteconnect not installed. Run: pip install kiteconnect", file=sys.stderr)
        sys.exit(1)

    api_key = os.environ.get("ZERODHA_API_KEY", "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN", "")
    if not api_key or not access_token:
        print(
            "ERROR: ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN must be set.\n"
            "  Run 'python scripts/zerodha_login.py' first, then export the token.",
            file=sys.stderr,
        )
        sys.exit(1)
    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)
    # Bound network timeouts so a hung TLS handshake can't stall the long backfill.
    try:
        kite.reqsession.timeout = (5, 15)
    except Exception:
        pass
    return kite


def _resolve_tokens(kite: Any, symbols: list[str], verbose: bool) -> dict[str, int]:
    """Map plain tradingsymbol → instrument_token via kite.instruments('NSE')."""
    t0 = time.monotonic()
    instruments = kite.instruments("NSE")
    _rate_sleep(t0)
    wanted = set(symbols)
    token_map: dict[str, int] = {}
    for inst in instruments:
        ts = inst.get("tradingsymbol")
        if ts in wanted and inst.get("instrument_type", "EQ") in ("EQ", ""):
            token_map[ts] = int(inst["instrument_token"])
    missing = [s for s in symbols if s not in token_map]
    if missing:
        print(f"  WARNING: no instrument token for: {missing}", file=sys.stderr)
    if verbose:
        print(f"  Resolved {len(token_map)}/{len(symbols)} tokens")
    return token_map


def _rate_sleep(call_start: float) -> None:
    """Maintain the 3 req/sec historical data budget."""
    remainder = _HIST_INTERVAL_SECONDS - (time.monotonic() - call_start)
    if remainder > 0:
        time.sleep(remainder)


# ── main fetch ───────────────────────────────────────────────────────────────


def fetch(
    kite: Any,
    symbols: list[str],
    intervals: list[str],
    start: date,
    end: date,
    base: Path,
    force: bool,
    verbose: bool,
) -> int:
    token_map = _resolve_tokens(kite, symbols, verbose)
    if not token_map:
        print("ERROR: no instrument tokens resolved — aborting.", file=sys.stderr)
        return 1

    total_bars = 0
    total_requests = 0
    manifest_entries: list[dict] = []

    for idx, symbol in enumerate(symbols, start=1):
        token = token_map.get(symbol)
        if token is None:
            continue
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
                    "symbol": symbol,
                    "interval": interval,
                    "instrument_token": token,
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
    print(f"Lake: {base / 'lake' / 'ohlcv'}")
    print("Backtest-only. Advisory. No live trading. No orders.")
    return 0


# ── self-test (no network) ───────────────────────────────────────────────────


class _StubKite:
    """Minimal in-memory Kite stand-in for --self-test (no network, no creds)."""

    def instruments(self, exchange: str) -> list[dict]:
        return [
            {"tradingsymbol": "RELIANCE", "instrument_token": 738561, "instrument_type": "EQ"},
            {"tradingsymbol": "INFY", "instrument_token": 408065, "instrument_type": "EQ"},
        ]

    def historical_data(self, token, from_date, to_date, interval, continuous=False):
        # Generate synthetic intraday bars: 09:15→15:29 IST on each weekday in range.
        step = {"minute": 1, "5minute": 5, "15minute": 15}.get(interval, 1)
        out = []
        d = from_date if isinstance(from_date, date) else date.fromisoformat(str(from_date)[:10])
        last = to_date if isinstance(to_date, date) else date.fromisoformat(str(to_date)[:10])
        price = 100.0 + (token % 50)
        while d <= last:
            if d.weekday() < 5:  # Mon-Fri
                minute_of_day = 9 * 60 + 15
                end_minute = 15 * 60 + 30
                while minute_of_day < end_minute:
                    hh, mm = divmod(minute_of_day, 60)
                    dt = datetime(d.year, d.month, d.day, hh, mm, tzinfo=_IST)
                    price += 0.1
                    out.append({
                        "date": dt,
                        "open": round(price, 2), "high": round(price + 0.5, 2),
                        "low": round(price - 0.5, 2), "close": round(price + 0.2, 2),
                        "volume": 1000 + minute_of_day,
                    })
                    minute_of_day += step
            d += timedelta(days=1)
        return out


def _self_test() -> int:
    import tempfile

    print("SELF-TEST: fetching synthetic intraday data (no network)...")
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        rc = fetch(
            kite=_StubKite(),
            symbols=["RELIANCE", "INFY"],
            intervals=["1m", "15m"],
            start=date(2024, 1, 1),
            end=date(2024, 1, 10),
            base=base,
            force=False,
            verbose=True,
        )
        assert rc == 0
        # Verify the lake is readable and schema-compatible with the loader.
        p = base / "lake" / "ohlcv" / "market=NSE" / "segment=EQ" / \
            "symbol=RELIANCE" / "interval=1m" / "year=2024" / "part-0.parquet"
        assert p.exists(), f"expected partition missing: {p}"
        df = pd.read_parquet(p)
        assert df["timestamp"].dt.tz is not None, "timestamps must be tz-aware IST"
        assert set(["open", "high", "low", "close", "volume", "symbol", "interval"]).issubset(df.columns)
        assert (df["interval"] == "1m").all()
        assert len(df) > 0
        # Manifest written
        man = json.loads(_manifest_path(base).read_text())
        assert man["source"] == SOURCE_NAME and man["trust_level"] == "HIGH"
        assert len(man["entries"]) >= 2
        print(f"  OK — {len(df)} RELIANCE 1m bars, manifest has "
              f"{len(man['entries'])} entries, schema valid.")
    print("SELF-TEST PASSED.")
    return 0


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch Zerodha Kite intraday history into the Parquet lake")
    ap.add_argument("--universe", choices=list(UNIVERSES), default="nifty50",
                    help="Predefined symbol universe (default: nifty50)")
    ap.add_argument("--symbols", help="Comma-separated tradingsymbols (overrides --universe)")
    ap.add_argument("--intervals", default="1m,5m,15m",
                    help="Comma-separated intervals from " + ",".join(_INTERVALS) + " (default: 1m,5m,15m)")
    ap.add_argument("--start", default="2022-01-01", help="Start date YYYY-MM-DD")
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
        if args.symbols else UNIVERSES[args.universe]
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
    print("QuantEmbrace — Zerodha Kite Intraday Fetch (backtesting lab Phase B)")
    print("Backtest-only. Limited-depth intraday for edge exploration. No live trading.")
    print("=" * 72)
    print(f"  Universe : {args.universe if not args.symbols else 'custom'} ({len(symbols)} symbols)")
    print(f"  Intervals: {intervals}")
    print(f"  Period   : {start} → {end}")
    print(f"  Lake     : {base / 'lake' / 'ohlcv'}")
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
        est_secs = total_reqs * _HIST_INTERVAL_SECONDS
        print(f"  Total ≈ {total_reqs} requests, ~{est_secs/60:.1f} min at 3 req/sec "
              f"(+ token resolution).")
        return 0

    kite = _build_kite()
    return fetch(kite, symbols, intervals, start, end, base, args.force, args.verbose)


if __name__ == "__main__":
    raise SystemExit(main())
