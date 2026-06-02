#!/usr/bin/env python3
"""
Candle Prefetch — pre-market historical data warm-up at the 3 req/sec budget.

Downloads the last N trading days of 1-minute OHLCV candles for the full
instrument watchlist and stores them in DynamoDB (quantembrace-candle-cache).
Run this before market open (recommended: 08:30-09:00 IST) so the strategy
engine starts the day with a warm candle cache instead of cold-reading history
on the first signal.

Why this matters:
    ORB (Opening Range Breakout) and Scalp 1m strategies require the last
    15-30 confirmed 1m candles to compute breakout levels and VWAP anchors.
    Without pre-fetching, the first 15 minutes of trading use only partial
    candles built from live ticks — which have higher noise and miss any
    pre-market gap.

Rate limit:
    Uses the SEPARATE 3 req/sec historical data budget.
    Does NOT consume order API tokens (10 req/sec budget unaffected).
    Enforces exactly 333ms between API calls.

Usage:
    # Warm up last 5 trading days for all instruments in the watchlist config
    python scripts/zerodha/candle_prefetch.py

    # Warm up specific instruments only
    python scripts/zerodha/candle_prefetch.py --symbols NSE:RELIANCE NSE:INFY NSE:TCS

    # Use 3 trading days, 5-minute candles
    python scripts/zerodha/candle_prefetch.py --days 3 --interval 5minute

    # Dry run — show what would be fetched without writing to DynamoDB
    python scripts/zerodha/candle_prefetch.py --dry-run

    # Verbose progress logging
    python scripts/zerodha/candle_prefetch.py --verbose

Requirements:
    pip install boto3 kiteconnect python-dotenv

Environment variables (from .env or environment):
    ZERODHA_API_KEY
    ZERODHA_ACCESS_TOKEN
    AWS_REGION              (default: ap-south-1)
    AWS_DYNAMODB_TABLE      (default: quantembrace-candle-cache)
"""

from __future__ import annotations

import argparse
from datetime import UTC, date, datetime, timedelta
import os
import sys
import time
from typing import Any

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    import boto3
except ImportError:
    print("ERROR: boto3 not installed. Run: pip install boto3", file=sys.stderr)
    sys.exit(1)

try:
    from kiteconnect import KiteConnect
except ImportError:
    print("ERROR: kiteconnect not installed. Run: pip install kiteconnect", file=sys.stderr)
    sys.exit(1)

# ── Constants ──────────────────────────────────────────────────────────────────

_HISTORICAL_INTERVAL_SECONDS = 1.0 / 3.0   # 333ms = 3 req/sec
_CANDLE_TTL_SECONDS          = 7_200         # 2-hour TTL in DynamoDB
_CANDLE_CACHE_BUCKET         = "ACTIVE"
_CANDLE_TTL_ATTRIBUTE        = "expires_at"
_DEFAULT_DAYS                = 5
_DEFAULT_INTERVAL            = "minute"

# Default NSE instrument watchlist — override via --symbols or set env WATCHLIST_SYMBOLS
_DEFAULT_WATCHLIST = [
    "NSE:RELIANCE", "NSE:INFY",    "NSE:TCS",     "NSE:HDFCBANK",
    "NSE:ICICIBANK","NSE:SBIN",    "NSE:AXISBANK", "NSE:KOTAKBANK",
    "NSE:WIPRO",    "NSE:BHARTIARTL",
]


def _rate_sleep(call_start: float) -> None:
    """Sleep to maintain 3 req/sec on the historical data budget."""
    elapsed   = time.monotonic() - call_start
    remainder = _HISTORICAL_INTERVAL_SECONDS - elapsed
    if remainder > 0:
        time.sleep(remainder)


def _ttl(offset_seconds: int = 0) -> int:
    return int(time.time()) + _CANDLE_TTL_SECONDS + offset_seconds


def _split_market_symbol(symbol: str) -> tuple[str, str]:
    """Return (market, instrument) from an exchange-qualified symbol."""
    if ":" in symbol:
        exchange, instrument = symbol.split(":", 1)
    else:
        exchange, instrument = "NSE", symbol
    market = "NSE" if exchange.upper() in {"NSE", "BSE", "NFO", "BFO"} else "US"
    return market, instrument


def _candle_open_time_iso(raw_dt: Any) -> str:
    """Normalize a Kite historical candle timestamp to UTC ISO-8601."""
    if isinstance(raw_dt, datetime):
        dt = raw_dt
    else:
        dt = datetime.fromisoformat(str(raw_dt))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    else:
        dt = dt.astimezone(UTC)
    return dt.isoformat()


def _trading_days_back(n: int) -> list[date]:
    """Return last N trading calendar days (Mon-Fri), excluding today."""
    days  = []
    delta = timedelta(days=1)
    d     = date.today() - delta
    while len(days) < n:
        if d.weekday() < 5:  # 0=Mon … 4=Fri
            days.append(d)
        d -= delta
    return list(reversed(days))


def _instrument_token_map(kite: KiteConnect, symbols: list[str]) -> dict[str, int]:
    """Build {exchange:symbol → instrument_token} map via kite.instruments()."""
    exchanges = list({s.split(":")[0] for s in symbols})
    token_map: dict[str, int] = {}
    seen_exchanges: set[str] = set()

    for exchange in exchanges:
        if exchange in seen_exchanges:
            continue
        seen_exchanges.add(exchange)
        t0 = time.monotonic()
        instruments = kite.instruments(exchange)
        _rate_sleep(t0)

        for inst in instruments:
            key = f"{exchange}:{inst['tradingsymbol']}"
            if key in symbols:
                token_map[key] = inst["instrument_token"]

    missing = [s for s in symbols if s not in token_map]
    if missing:
        print(f"  WARNING: no instrument token found for: {missing}", file=sys.stderr)

    return token_map


def _fetch_candles(
    kite: KiteConnect,
    instrument_token: int,
    from_date: date,
    to_date: date,
    interval: str,
) -> list[dict[str, Any]]:
    """Fetch candles for one instrument/date range from Zerodha historical API."""
    return kite.historical_data(
        instrument_token,
        from_date=from_date,
        to_date=to_date,
        interval=interval,
        continuous=False,
    )


def _write_candle_batch(
    dynamo,
    table_name: str,
    symbol: str,
    interval: str,
    candles: list[dict[str, Any]],
    verbose: bool,
) -> int:
    """Batch-write candles to DynamoDB candle-cache table. Returns count written."""
    written = 0

    table = dynamo.Table(table_name)
    with table.batch_writer() as batch:
        for raw in candles:
            market, instrument = _split_market_symbol(symbol)
            candle_open_time = _candle_open_time_iso(raw.get("date"))
            pk = f"{market}#{instrument}#{interval}#{candle_open_time}"
            ttl = _ttl()

            item = {
                "PK":               pk,
                "cache_bucket":     _CANDLE_CACHE_BUCKET,
                "market":           market,
                "instrument":       instrument,
                "interval":         interval,
                "candle_open_time": candle_open_time,
                "open":             str(float(raw.get("open",   0))),
                "high":             str(float(raw.get("high",   0))),
                "low":              str(float(raw.get("low",    0))),
                "close":            str(float(raw.get("close",  0))),
                "volume":           str(int(raw.get("volume",   0))),
                "source":           "prefetch",
                "prefetched_at":    datetime.now(tz=UTC).isoformat(),
                _CANDLE_TTL_ATTRIBUTE: ttl,
            }
            batch.put_item(Item=item)
            written += 1

    if verbose and written > 0:
        print(f"    DynamoDB: {written} candles written for {symbol}/{interval}")

    return written


def run(
    symbols: list[str],
    days: int,
    interval: str,
    region: str,
    table_name: str,
    dry_run: bool,
    verbose: bool,
) -> None:
    """Main prefetch logic."""
    api_key      = os.environ.get("ZERODHA_API_KEY",     "")
    access_token = os.environ.get("ZERODHA_ACCESS_TOKEN","")

    if not api_key or not access_token:
        print(
            "ERROR: ZERODHA_API_KEY and ZERODHA_ACCESS_TOKEN must be set.\n"
            "  export ZERODHA_API_KEY=your_key\n"
            "  export ZERODHA_ACCESS_TOKEN=your_token",
            file=sys.stderr,
        )
        sys.exit(1)

    kite = KiteConnect(api_key=api_key)
    kite.set_access_token(access_token)

    session   = boto3.Session(region_name=region)
    dynamo    = session.resource("dynamodb")

    trading_days = _trading_days_back(days)
    from_date    = trading_days[0]
    to_date      = trading_days[-1]

    print("Candle Prefetch")
    print(f"  Symbols:    {len(symbols)}")
    print(f"  Interval:   {interval}")
    print(f"  Date range: {from_date} → {to_date} ({days} trading days)")
    print(f"  DynamoDB:   {table_name} (region={region})")
    print(f"  Dry run:    {dry_run}")
    print()

    # Build instrument token map (uses 1 API call per exchange)
    print("Resolving instrument tokens...")
    t0 = time.monotonic()
    token_map = _instrument_token_map(kite, symbols)
    elapsed = time.monotonic() - t0
    print(f"  Resolved {len(token_map)}/{len(symbols)} tokens in {elapsed:.1f}s\n")

    total_candles  = 0
    total_requests = 0
    errors         = []

    for idx, symbol in enumerate(symbols, start=1):
        token = token_map.get(symbol)
        if token is None:
            print(f"  [{idx:3d}/{len(symbols)}] SKIP  {symbol}  (no token)")
            continue

        call_start = time.monotonic()
        try:
            if verbose:
                print(f"  [{idx:3d}/{len(symbols)}] FETCH {symbol}  token={token}  "
                      f"{from_date}→{to_date}  interval={interval}")

            candles = _fetch_candles(kite, token, from_date, to_date, interval)
            total_requests += 1

            if not candles:
                print(f"  [{idx:3d}/{len(symbols)}] EMPTY {symbol}  (no candles returned)")
            else:
                total_candles += len(candles)
                if verbose:
                    print(f"    {len(candles)} candles received")

                if not dry_run:
                    _write_candle_batch(dynamo, table_name, symbol, interval, candles, verbose)
                else:
                    first = candles[0].get("date")
                    last  = candles[-1].get("date")
                    print(f"  [{idx:3d}/{len(symbols)}] DRY   {symbol}  "
                          f"{len(candles)} candles  {first} → {last}")

        except Exception as exc:
            errors.append((symbol, str(exc)))
            print(f"  [{idx:3d}/{len(symbols)}] ERROR {symbol}: {exc}", file=sys.stderr)

        _rate_sleep(call_start)

    print()
    print("Prefetch complete")
    print(f"  API calls:      {total_requests}")
    print(f"  Total candles:  {total_candles}")
    print(f"  Written:        {'0 (dry run)' if dry_run else str(total_candles)}")
    print(f"  Errors:         {len(errors)}")
    if errors:
        for sym, msg in errors:
            print(f"    {sym}: {msg}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pre-market candle cache warm-up at 3 req/sec historical data budget"
    )
    parser.add_argument(
        "--symbols", nargs="+", default=None,
        help="Space-separated list of instruments e.g. NSE:RELIANCE NSE:INFY",
    )
    parser.add_argument("--days",    type=int, default=_DEFAULT_DAYS,     help="Trading days to prefetch")
    parser.add_argument("--interval",         default=_DEFAULT_INTERVAL,  help="Candle interval (minute, 5minute, etc.)")
    parser.add_argument("--region",           default="ap-south-1",       help="AWS region")
    parser.add_argument("--table",            default="quantembrace-candle-cache", help="DynamoDB table name")
    parser.add_argument("--dry-run",  action="store_true",                help="Show what would be fetched without writing")
    parser.add_argument("--verbose",  action="store_true",                help="Verbose per-instrument logging")
    args = parser.parse_args()

    symbols_env = os.environ.get("WATCHLIST_SYMBOLS", "")
    if args.symbols:
        symbols = args.symbols
    elif symbols_env:
        symbols = [s.strip() for s in symbols_env.split(",") if s.strip()]
    else:
        symbols = _DEFAULT_WATCHLIST

    run(symbols, args.days, args.interval, args.region, args.table, args.dry_run, args.verbose)


if __name__ == "__main__":
    main()
