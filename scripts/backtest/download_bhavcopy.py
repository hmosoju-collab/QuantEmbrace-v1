#!/usr/bin/env python3
"""
QuantEmbrace — NSE Bhavcopy Bulk Downloader
Downloads sec_bhavdata_full CSVs (2016–2025) from NSE archives and normalizes
them into the local Parquet lake compatible with the QuantEmbrace backtesting lab.

IMPORTANT: Run this on your local Mac — the NSE archives require a real browser
session that the sandbox cannot emulate. This script handles the bot-shield.

Install deps first:
    pip install requests pandas pyarrow tqdm

Usage:
    # Download everything 2016–2025 (default)
    python scripts/backtest/download_bhavcopy.py

    # Download a specific year only (test first)
    python scripts/backtest/download_bhavcopy.py --start 2024-01-01 --end 2024-12-31

    # Resume interrupted download (idempotent — skips files already downloaded)
    python scripts/backtest/download_bhavcopy.py

Output layout (compatible with DataCatalog / data_loader.py):
    <repo>/backtest-data/
        raw/bhavcopy/{ingest_date=YYYY-MM-DD}/
            sec_bhavdata_full_DDMMYYYY.csv     # verbatim
            _manifest.json                     # sha256, row count, trust
        lake/ohlcv/market=NSE/segment=EQ/symbol={SYM}/interval=1d/year={YYYY}/
            part-0.parquet                     # canonical Parquet, IST tz-aware

Backtest-only. No broker APIs. No live/paper trading. No AWS needed for Phase 1.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import queue
import sys
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# ── repo paths ────────────────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"

# ── NSE config ────────────────────────────────────────────────────────────────
NSE_HOME = "https://www.nseindia.com"
NSE_ARCHIVE_URL = (
    "https://nsearchives.nseindia.com/products/content/sec_bhavdata_full_{date}.csv"
)
EQUITY_MASTER_URL = "https://www.nseindia.com/content/equities/EQUITY_L.csv"

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": "https://www.nseindia.com/",
}

# ── Parquet canonical schema ───────────────────────────────────────────────────
PARQUET_SCHEMA = pa.schema([
    ("timestamp", pa.timestamp("us", tz="Asia/Kolkata")),
    ("symbol", pa.string()),
    ("isin", pa.string()),          # filled later from EQUITY_L.csv; null for now
    ("market", pa.string()),
    ("segment", pa.string()),
    ("interval", pa.string()),
    ("open", pa.float64()),
    ("high", pa.float64()),
    ("low", pa.float64()),
    ("close", pa.float64()),
    ("volume", pa.int64()),
    ("prev_close", pa.float64()),
    ("delivery_qty", pa.float64()),
    ("delivery_pct", pa.float64()),
    ("source", pa.string()),
    ("trust_level", pa.string()),
])

# ── NSE known holidays (weekdays to skip) — extend as needed ─────────────────
# This list covers major NSE holidays. 404 on non-listed holidays is safe — we skip.
# Source: NSE holiday list, updated for 2016–2026.
NSE_HOLIDAYS: set[date] = set()  # Script handles 404 gracefully; no need for exhaustive list


def _prime_session(session: requests.Session, verbose: bool = True) -> bool:
    """Send priming GET to nseindia.com to acquire session cookies (bot-shield bypass)."""
    if verbose:
        print("Priming NSE session (bot-shield bypass)...", end=" ", flush=True)
    try:
        resp = session.get(NSE_HOME, timeout=5, headers=BROWSER_HEADERS)
        resp.raise_for_status()
        if verbose:
            print(f"OK (status={resp.status_code}, cookies={len(session.cookies)})")
        time.sleep(1.5)  # settle
        return True
    except Exception as e:
        if verbose:
            print(f"WARN: {e}")
        return False


def _build_session() -> requests.Session:
    """Create a requests session with browser-like headers and NSE cookies."""
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    # Run priming in a daemon thread so a hung TLS handshake can't block the main loop.
    # The daemon thread is abandoned if it exceeds the timeout; the process exits cleanly.
    result_q: queue.Queue = queue.Queue()

    def _prime_worker():
        try:
            ok = _prime_session(s, verbose=False)
            result_q.put(("ok", ok))
        except Exception as e:
            result_q.put(("err", e))

    t = threading.Thread(target=_prime_worker, daemon=True)
    t.start()
    try:
        kind, val = result_q.get(timeout=8)
        if kind == "ok" and val:
            print("Priming NSE session (bot-shield bypass)... OK")
        else:
            print(f"Priming NSE session (bot-shield bypass)... WARN: {val if kind == 'err' else 'non-200'}")
    except queue.Empty:
        print("Priming NSE session (bot-shield bypass)... WARN: timed out — continuing without cookies")
    return s


def _trading_days(start: date, end: date) -> list[date]:
    """Return all weekdays between start and end inclusive (skip Sat/Sun; 404 = holiday)."""
    days = []
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon–Fri
            days.append(current)
        current += timedelta(days=1)
    return days


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _raw_dir(base: Path, d: date) -> Path:
    return base / "raw" / "bhavcopy" / f"ingest_date={d.isoformat()}"


def _lake_dir(base: Path, symbol: str, year: int) -> Path:
    sym = symbol.replace("/", "_").replace("&", "_").strip()
    return base / "lake" / "ohlcv" / "market=NSE" / "segment=EQ" / f"symbol={sym}" / "interval=1d" / f"year={year}"


def download_one(session: requests.Session, d: date, base: Path,
                 dry_run: bool = False) -> Optional[dict]:
    """Download a single day's Bhavcopy. Returns manifest dict or None on skip/failure."""
    date_str = d.strftime("%d%m%Y")   # DDMMYYYY
    url = NSE_ARCHIVE_URL.format(date=date_str)
    raw_dir = _raw_dir(base, date.today())  # group by ingest date (today)
    dest_file = raw_dir / f"sec_bhavdata_full_{date_str}.csv"
    manifest_file = raw_dir / "_manifest.json"

    # Idempotent: skip if already downloaded
    if dest_file.exists() and manifest_file.exists():
        with open(manifest_file) as f:
            mf = json.load(f)
        # Check this specific trading date is recorded
        if any(e.get("trading_date") == d.isoformat() for e in mf.get("files", [])):
            return None  # already done

    if dry_run:
        print(f"  [dry-run] would download {url}")
        return None

    try:
        resp = session.get(url, timeout=30, headers=BROWSER_HEADERS)
        if resp.status_code == 404:
            # Holiday or non-trading day — expected, not an error
            return None
        if resp.status_code == 403:
            # Bot-shield kicked in — re-prime and retry once
            print(f"\n  [WARN] 403 on {url} — re-priming session...")
            _prime_session(session, verbose=False)
            time.sleep(3)
            resp = session.get(url, timeout=30, headers=BROWSER_HEADERS)

        resp.raise_for_status()
        raw_bytes = resp.content
        sha = _sha256_bytes(raw_bytes)
        raw_dir.mkdir(parents=True, exist_ok=True)
        dest_file.write_bytes(raw_bytes)

        # Update manifest
        entry = {
            "trading_date": d.isoformat(),
            "filename": dest_file.name,
            "url": url,
            "sha256": sha,
            "size_bytes": len(raw_bytes),
            "downloaded_at": datetime.utcnow().isoformat() + "Z",
            "source": "bhavcopy",
            "trust_level": "HIGH",
        }
        existing = []
        if manifest_file.exists():
            with open(manifest_file) as f:
                existing = json.load(f).get("files", [])
        existing.append(entry)
        manifest_file.write_text(json.dumps({"files": existing}, indent=2))
        return entry

    except requests.exceptions.RequestException as e:
        print(f"\n  [ERROR] {d} — {e}")
        return None


def _parse_bhavcopy(csv_bytes: bytes, trading_date: date) -> pd.DataFrame:
    """Parse a sec_bhavdata_full CSV into canonical DataFrame (EQ series only)."""
    import io

    df = pd.read_csv(
        io.BytesIO(csv_bytes),
        dtype=str,
        skipinitialspace=True,   # strips leading whitespace from headers/values
    )

    # Normalise column names (strip whitespace, uppercase)
    df.columns = [c.strip().upper() for c in df.columns]

    # Filter EQ equity series only
    if "SERIES" not in df.columns:
        return pd.DataFrame()
    df = df[df["SERIES"].str.strip() == "EQ"].copy()

    if df.empty:
        return pd.DataFrame()

    # Map to canonical columns
    col_map = {
        "SYMBOL":       "symbol",
        "OPEN_PRICE":   "open",
        "HIGH_PRICE":   "high",
        "LOW_PRICE":    "low",
        "CLOSE_PRICE":  "close",
        "TTL_TRD_QNTY": "volume",
        "PREV_CLOSE":   "prev_close",
        "DELIV_QTY":    "delivery_qty",
        "DELIV_PER":    "delivery_pct",
    }
    df = df.rename(columns=col_map)
    df["symbol"] = df["symbol"].str.strip()

    # Build IST-aware timestamp (daily bar closes at 15:30 IST)
    ts = pd.Timestamp(trading_date.isoformat() + " 15:30:00").tz_localize("Asia/Kolkata")
    df["timestamp"] = ts

    # Numeric conversion
    for col in ["open", "high", "low", "close", "prev_close"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "volume" in df.columns:
        df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    if "delivery_qty" in df.columns:
        df["delivery_qty"] = pd.to_numeric(df["delivery_qty"], errors="coerce").fillna(0.0)
    if "delivery_pct" in df.columns:
        df["delivery_pct"] = pd.to_numeric(df["delivery_pct"], errors="coerce")

    # Add canonical metadata columns
    df["isin"] = None
    df["market"] = "NSE"
    df["segment"] = "EQ"
    df["interval"] = "1d"
    df["source"] = "bhavcopy"
    df["trust_level"] = "HIGH"

    # Drop rows with null OHLC
    df = df.dropna(subset=["open", "high", "low", "close"])

    # Basic OHLC sanity: low <= open, close <= high
    valid = (
        (df["low"] <= df["open"]) &
        (df["low"] <= df["close"]) &
        (df["high"] >= df["open"]) &
        (df["high"] >= df["close"]) &
        (df["open"] > 0) &
        (df["close"] > 0)
    )
    bad = (~valid).sum()
    if bad:
        print(f"  [WARN] {bad} rows failed OHLC validation on {trading_date} — dropping")
    df = df[valid].copy()

    return df


def normalize_to_parquet(base: Path, year: int, verbose: bool = True) -> dict:
    """
    Read all downloaded Bhavcopy CSVs for a given year, normalize, and write
    per-symbol Parquet files to the lake.
    Returns summary dict.
    """
    if verbose:
        print(f"\nNormalizing year {year}...")

    # Collect all trading days for this year that have raw CSVs
    raw_base = base / "raw" / "bhavcopy"
    symbol_frames: dict[str, list[pd.DataFrame]] = {}

    # Scan all ingest_date= dirs for CSVs matching this year
    csv_files = sorted(raw_base.rglob(f"sec_bhavdata_full_??{year:04d}*.csv"))
    # Also check DDMMYYYY pattern where year is at the end
    # Pattern: sec_bhavdata_full_DDMMYYYY.csv
    year_csvs = []
    for f in sorted(raw_base.rglob("sec_bhavdata_full_*.csv")):
        name = f.stem  # sec_bhavdata_full_DDMMYYYY
        parts = name.split("_")
        if len(parts) >= 4:
            date_part = parts[-1]  # DDMMYYYY
            if len(date_part) == 8 and date_part[4:] == str(year):
                year_csvs.append(f)

    if not year_csvs:
        if verbose:
            print(f"  No CSVs found for {year} — skipping")
        return {"year": year, "symbols": 0, "rows": 0, "days": 0}

    if verbose:
        print(f"  Found {len(year_csvs)} CSV files for {year}")

    rows_total = 0
    days_processed = 0
    for csv_path in sorted(year_csvs):
        date_part = csv_path.stem.split("_")[-1]  # DDMMYYYY
        try:
            d = date(int(date_part[4:8]), int(date_part[2:4]), int(date_part[0:2]))
        except ValueError:
            continue

        raw_bytes = csv_path.read_bytes()
        df = _parse_bhavcopy(raw_bytes, d)
        if df.empty:
            continue

        for sym, grp in df.groupby("symbol"):
            symbol_frames.setdefault(sym, []).append(grp)
        rows_total += len(df)
        days_processed += 1

    if verbose:
        print(f"  Parsed {days_processed} trading days, {rows_total} EQ rows, {len(symbol_frames)} symbols")

    # Write one Parquet file per symbol for this year
    written = 0
    for sym, frames in symbol_frames.items():
        combined = pd.concat(frames, ignore_index=True)
        combined = combined.sort_values("timestamp")

        # Select only PARQUET_SCHEMA columns (in order)
        schema_cols = [f.name for f in PARQUET_SCHEMA]
        for col in schema_cols:
            if col not in combined.columns:
                combined[col] = None
        combined = combined[schema_cols]

        # Ensure string columns are not categorical (avoids pyarrow dict/string conflict)
        for col in ["symbol", "isin", "market", "segment", "interval", "source", "trust_level"]:
            if col in combined.columns:
                combined[col] = combined[col].astype(str).replace("None", None)

        # Convert to Arrow (let pyarrow infer types — avoids dict vs string conflicts)
        table = pa.Table.from_pandas(combined, preserve_index=False)

        lake_dir = _lake_dir(base, sym, year)
        lake_dir.mkdir(parents=True, exist_ok=True)
        out_path = lake_dir / "part-0.parquet"
        pq.write_table(table, out_path, compression="snappy")
        written += 1

    if verbose:
        print(f"  Wrote {written} symbol Parquet files to lake")

    return {"year": year, "symbols": written, "rows": rows_total, "days": days_processed}


def download_equity_master(session: requests.Session, base: Path) -> None:
    """Download EQUITY_L.csv (SYMBOL→ISIN reference) once."""
    dest = base / "reference" / "symbol_map" / "equity_l.csv"
    if dest.exists():
        print("EQUITY_L.csv already present — skipping")
        return
    print("Downloading EQUITY_L.csv (SYMBOL→ISIN reference)...", end=" ", flush=True)
    try:
        resp = session.get(EQUITY_MASTER_URL, timeout=8)  # short timeout — main site may bot-block
        resp.raise_for_status()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(resp.content)
        print(f"OK ({len(resp.content):,} bytes)")
    except Exception as e:
        print(f"WARN: {e}")


def main() -> int:
    p = argparse.ArgumentParser(
        description="Download NSE Bhavcopy (2016–2025) and normalize to Parquet lake"
    )
    p.add_argument("--start", default="2016-01-01", help="Start date YYYY-MM-DD")
    p.add_argument("--end", default="2025-12-31", help="End date YYYY-MM-DD")
    p.add_argument("--base", default=str(_DEFAULT_BASE), help="Lake base directory")
    p.add_argument("--normalize-only", action="store_true",
                   help="Skip download, only normalize already-downloaded CSVs")
    p.add_argument("--download-only", action="store_true",
                   help="Skip normalization step after download")
    p.add_argument("--rate-limit", type=float, default=1.0,
                   help="Seconds between requests (default 1.0)")
    p.add_argument("--dry-run", action="store_true",
                   help="Print what would be downloaded, don't fetch")
    p.add_argument("--year", type=int, default=None,
                   help="Normalize only this year (shortcut for --normalize-only --start/--end)")
    args = p.parse_args()

    base = Path(args.base)
    base.mkdir(parents=True, exist_ok=True)

    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    if args.year and args.normalize_only:
        # Normalize a single year
        result = normalize_to_parquet(base, args.year)
        print(f"\nDone: {result}")
        return 0

    # ── PHASE 1: DOWNLOAD ────────────────────────────────────────────────────
    if not args.normalize_only:
        session = _build_session()
        download_equity_master(session, base)

        days = _trading_days(start_date, end_date)
        total = len(days)
        downloaded = 0
        skipped = 0
        failed = 0

        print(f"\nDownloading {total} weekday dates ({start_date} → {end_date})")
        print("(404s on market holidays are expected and skipped silently)\n")

        try:
            for i, d in enumerate(days):
                date_str = d.strftime("%d%m%Y")
                # Check if already exists
                raw_dir = _raw_dir(base, date.today())
                existing = raw_dir / f"sec_bhavdata_full_{date_str}.csv"
                already_have = any(
                    f.name == f"sec_bhavdata_full_{date_str}.csv"
                    for f in base.rglob("sec_bhavdata_full_*.csv")
                )
                if already_have:
                    skipped += 1
                    if i % 100 == 0:
                        print(f"  [{i+1}/{total}] {d} skipped (already downloaded) — "
                              f"{downloaded} new, {skipped} skipped, {failed} errors")
                    continue

                result = download_one(session, d, base, dry_run=args.dry_run)

                if result is not None:
                    downloaded += 1
                    print(f"  [{i+1}/{total}] {d} ✓ ({result['size_bytes']:,} bytes, "
                          f"sha={result['sha256'][:10]}…)")
                else:
                    skipped += 1

                time.sleep(args.rate_limit)

                # Re-prime session every 200 requests to keep cookies fresh
                if (i + 1) % 200 == 0 and not args.dry_run:
                    print(f"\n  [Re-priming session at request {i+1}...]")
                    _prime_session(session, verbose=False)
                    time.sleep(2)

        except KeyboardInterrupt:
            print(f"\n\nInterrupted at {d}. Downloaded: {downloaded}, skipped: {skipped}. "
                  "Re-run to resume (idempotent).")
            return 1

        print(f"\n✓ Download complete: {downloaded} new files, {skipped} skipped, {failed} errors")

        if args.download_only:
            return 0

    # ── PHASE 2: NORMALIZE ───────────────────────────────────────────────────
    print("\n─── Normalizing to Parquet lake ───")
    years = range(start_date.year, end_date.year + 1)
    total_symbols = 0
    total_rows = 0

    for year in years:
        result = normalize_to_parquet(base, year)
        total_symbols = max(total_symbols, result["symbols"])
        total_rows += result["rows"]

    # ── SUMMARY ──────────────────────────────────────────────────────────────
    print(f"""
╔══════════════════════════════════════════════════════════════╗
║         BHAVCOPY DOWNLOAD + NORMALIZE COMPLETE               ║
╠══════════════════════════════════════════════════════════════╣
║  Period:       {start_date} → {end_date}
║  Total rows:   {total_rows:,}
║  Symbols:      up to {total_symbols} per year (EQ series)
║  Lake base:    {base}
║  Format:       Parquet/Snappy, IST tz-aware, interval=1d
║  Trust:        HIGH (source=bhavcopy, official NSE archives)
╠══════════════════════════════════════════════════════════════╣
║  NEXT STEPS:                                                  ║
║  1. Run validate:                                             ║
║     python scripts/backtest/validate_s3_nse_history.py \\    ║
║       --source-path backtest-data/lake --source-name bhavcopy║
║  2. Run backtests:                                            ║
║     python scripts/backtest/run_full_backtest_aws.py \\      ║
║       --data-path backtest-data/lake --strategies all        ║
╚══════════════════════════════════════════════════════════════╝
""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
