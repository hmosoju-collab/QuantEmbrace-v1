#!/usr/bin/env python3
"""Fetch NSE quarterly results (earnings) dates into a canonical CSV for H5 PEAD study.

NSE publishes "Board Meeting to consider Results" announcements in its corporate filings.
This script fetches them in bulk using NSE's session-primed API (same bot-shield approach
as download_bhavcopy.py) and normalises them into a flat CSV:

    backtest-data/reference/earnings_calendar.csv
    Columns: symbol, announce_date (YYYY-MM-DD), purpose (Q1/Q2/Q3/Q4/Annual), source

The CSV is consumed by run_pead_study.py.  If the NSE API is unavailable or rate-limits,
the script falls back to a BSE bulk download (requires ISIN→NSE symbol mapping from
EQUITY_L.csv which download_bhavcopy.py already fetches).

Usage:
    # Fetch 2019–2025 (default; extend once pre-2019 lake is ready)
    python scripts/backtest/fetch_earnings_calendar.py

    # Specific years
    python scripts/backtest/fetch_earnings_calendar.py --start 2020-01-01 --end 2022-12-31

    # Check what's already in the calendar
    python scripts/backtest/fetch_earnings_calendar.py --stats

Backtest/research only. No broker. No live/paper state. Advisory.
"""

from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from datetime import date, timedelta
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

import pandas as pd
import requests

# ── output paths ──────────────────────────────────────────────────────────────
OUT_CSV = _REPO / "backtest-data" / "reference" / "earnings_calendar.csv"
EQUITY_L = _REPO / "backtest-data" / "reference" / "symbol_map" / "equity_l.csv"

# ── NSE API ───────────────────────────────────────────────────────────────────
NSE_HOME = "https://www.nseindia.com"
NSE_ARCHIVE_EQUITY_L = "https://archives.nseindia.com/content/equities/EQUITY_L.csv"
NSE_ANNOUNCE_URL = (
    "https://www.nseindia.com/api/corporates-announcements"
    "?index=equities&symbol=&from_date={from_d}&to_date={to_d}&category=Board+Meeting+Results"
)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-financial-results",
    "X-Requested-With": "XMLHttpRequest",
}

_QUARTER_KEYWORDS = {
    "Q1": ("q1", "first quarter", "june"),
    "Q2": ("q2", "second quarter", "september"),
    "Q3": ("q3", "third quarter", "december"),
    "Q4": ("q4", "fourth quarter", "annual", "full year", "march"),
}


def _prime_session(session: requests.Session) -> bool:
    try:
        r = session.get(NSE_HOME, timeout=8, headers=BROWSER_HEADERS)
        r.raise_for_status()
        time.sleep(1.5)
        return True
    except Exception:
        return False


def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(BROWSER_HEADERS)
    q: queue.Queue = queue.Queue()

    def _w():
        try:
            q.put(("ok", _prime_session(s)))
        except Exception as e:
            q.put(("err", e))

    t = threading.Thread(target=_w, daemon=True)
    t.start()
    try:
        kind, val = q.get(timeout=10)
        print(f"NSE session priming: {'OK' if kind == 'ok' and val else 'WARN (continuing)'}")
    except queue.Empty:
        print("NSE session priming: TIMEOUT (continuing)")
    return s


def _infer_quarter(purpose: str) -> str:
    low = purpose.lower()
    for q, kws in _QUARTER_KEYWORDS.items():
        if any(k in low for k in kws):
            return q
    return "Unknown"


# ── NSE announcements API ─────────────────────────────────────────────────────


def _fetch_nse_quarter(session: requests.Session, start: date, end: date,
                       rate: float = 1.0) -> list[dict]:
    """Fetch one quarter's worth of board-meeting-results announcements from NSE."""
    url = NSE_ANNOUNCE_URL.format(
        from_d=start.strftime("%d-%m-%Y"),
        to_d=end.strftime("%d-%m-%Y"),
    )
    try:
        r = session.get(url, timeout=15)
        if r.status_code == 403:
            print(f"  WARN: 403 on {start}→{end} — re-priming...")
            _prime_session(session)
            time.sleep(3)
            r = session.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()
        time.sleep(rate)
        return data if isinstance(data, list) else []
    except Exception as e:
        print(f"  ERROR {start}→{end}: {e}")
        return []


def fetch_nse(start_date: date, end_date: date, rate: float = 1.5) -> pd.DataFrame:
    """Fetch all board-meeting results announcements from NSE in quarterly chunks."""
    session = _build_session()
    rows = []
    # Chunk by calendar quarter to avoid huge responses
    cur = start_date
    while cur <= end_date:
        # end of quarter
        q_month = ((cur.month - 1) // 3 + 1) * 3
        q_end = date(cur.year + (1 if q_month == 12 else 0),
                     (q_month % 12) + 1, 1) - timedelta(days=1)
        chunk_end = min(q_end, end_date)
        print(f"  Fetching {cur} → {chunk_end} ...", end=" ", flush=True)
        records = _fetch_nse_quarter(session, cur, chunk_end, rate)
        print(f"{len(records)} records")
        rows.extend(records)
        cur = chunk_end + timedelta(days=1)

    if not rows:
        return pd.DataFrame()

    df = pd.json_normalize(rows)
    # NSE columns vary; typical: 'symbol', 'an_dt' or 'bm_date', 'subject', 'purpose', 'desc'
    # Try to normalise
    sym_col = next((c for c in df.columns if "symbol" in c.lower()), None)
    dt_col = next((c for c in df.columns if c.lower() in ("an_dt", "bm_date", "date",
                                                            "announcement_date")), None)
    sub_col = next((c for c in df.columns if c.lower() in ("subject", "desc",
                                                             "purpose", "details")), None)
    if not sym_col or not dt_col:
        print(f"  WARN: Unexpected NSE response columns: {list(df.columns)[:10]}")
        return pd.DataFrame()

    out = pd.DataFrame()
    out["symbol"] = df[sym_col].str.strip().str.upper()
    out["announce_date"] = pd.to_datetime(df[dt_col], dayfirst=True, errors="coerce").dt.date
    out["purpose"] = df[sub_col].str.strip() if sub_col else "Unknown"
    out["quarter"] = out["purpose"].map(_infer_quarter)
    out["source"] = "NSE"
    return out.dropna(subset=["symbol", "announce_date"])


# ── BSE fallback ──────────────────────────────────────────────────────────────


def _isin_to_nse_symbol() -> dict[str, str]:
    """Build ISIN→NSE-symbol map from EQUITY_L.csv."""
    if not EQUITY_L.exists() or EQUITY_L.stat().st_size == 0:
        return {}
    try:
        df = pd.read_csv(EQUITY_L, dtype=str)
    except Exception:
        return {}
    df.columns = [c.strip().upper() for c in df.columns]
    sym_col = next((c for c in df.columns if "SYMBOL" in c), None)
    isin_col = next((c for c in df.columns if "ISIN" in c), None)
    if not sym_col or not isin_col:
        return {}
    return dict(zip(df[isin_col].str.strip(), df[sym_col].str.strip()))


def fetch_bse_fallback(start_date: date, end_date: date) -> pd.DataFrame:
    """Fallback: scrape BSE quarterly results calendar."""
    isin_map = _isin_to_nse_symbol()
    if not isin_map:
        print("  BSE fallback: no ISIN map (run download_bhavcopy.py first)")
        return pd.DataFrame()

    session = _build_session()
    rows = []
    year = start_date.year
    while year <= end_date.year:
        for q in ("Q1", "Q2", "Q3", "Q4"):
            url = (
                f"https://api.bseindia.com/BseIndiaAPI/api/ResultCalendar/w"
                f"?type=Q&fyear={year}&fquarter={q}&fstatus="
            )
            try:
                r = session.get(url, timeout=10)
                r.raise_for_status()
                data = r.json()
                items = data.get("Table", []) or []
                for item in items:
                    isin = str(item.get("ISIN_CODE", "")).strip()
                    sym = isin_map.get(isin, "")
                    dt_raw = item.get("BOARD_MEETING_DATE", "")
                    try:
                        dt = pd.to_datetime(dt_raw, dayfirst=True).date()
                    except Exception:
                        continue
                    if sym and start_date <= dt <= end_date:
                        rows.append({"symbol": sym.upper(),
                                     "announce_date": dt,
                                     "purpose": f"Results {q} {year}",
                                     "quarter": q,
                                     "source": "BSE"})
                time.sleep(0.5)
            except Exception as e:
                print(f"  BSE {year}/{q} error: {e}")
        year += 1

    return pd.DataFrame(rows) if rows else pd.DataFrame()


# ── merge + deduplicate ────────────────────────────────────────────────────────


def _merge_and_save(new_df: pd.DataFrame) -> pd.DataFrame:
    """Merge new records with existing calendar, deduplicate, and save."""
    existing = pd.DataFrame()
    if OUT_CSV.exists():
        existing = pd.read_csv(OUT_CSV, parse_dates=["announce_date"])
        existing["announce_date"] = pd.to_datetime(existing["announce_date"]).dt.date

    if new_df.empty:
        print("  No new records fetched.")
        return existing

    combined = pd.concat([existing, new_df], ignore_index=True)
    combined["announce_date"] = pd.to_datetime(combined["announce_date"]).dt.date
    before = len(combined)
    combined = combined.drop_duplicates(subset=["symbol", "announce_date"])
    combined = combined.sort_values(["symbol", "announce_date"])
    added = len(combined) - len(existing)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    combined.to_csv(OUT_CSV, index=False)
    print(f"\n  Saved {len(combined)} total records (+{added} new) → {OUT_CSV}")
    return combined


def _stats(df: pd.DataFrame) -> None:
    if df.empty:
        print("  Calendar is empty.")
        return
    print(f"  Total records:  {len(df):,}")
    print(f"  Unique symbols: {df['symbol'].nunique():,}")
    min_d = df["announce_date"].min()
    max_d = df["announce_date"].max()
    print(f"  Date range:     {min_d} → {max_d}")
    if "source" in df.columns:
        print(f"  Sources:        {dict(df['source'].value_counts())}")
    if "quarter" in df.columns:
        print(f"  By quarter:     {dict(df['quarter'].value_counts())}")


# ── main ──────────────────────────────────────────────────────────────────────


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fetch NSE quarterly results dates for the H5 PEAD study"
    )
    ap.add_argument("--start", default="2019-01-01", help="Start date YYYY-MM-DD")
    ap.add_argument("--end", default="2025-12-31", help="End date YYYY-MM-DD")
    ap.add_argument("--source", choices=["nse", "bse", "both"], default="both")
    ap.add_argument("--rate", type=float, default=1.5,
                    help="Seconds between NSE requests (default 1.5)")
    ap.add_argument("--stats", action="store_true",
                    help="Print stats for the existing calendar and exit")
    args = ap.parse_args()

    if args.stats:
        df = pd.read_csv(OUT_CSV, parse_dates=["announce_date"]) if OUT_CSV.exists() \
            else pd.DataFrame()
        _stats(df)
        return 0

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    print(f"Fetching earnings calendar {start} → {end} (source={args.source})")
    print(f"Output: {OUT_CSV}")

    all_rows: list[pd.DataFrame] = []

    if args.source in ("nse", "both"):
        print("\n── NSE Announcements API ──")
        nse_df = fetch_nse(start, end, rate=args.rate)
        if not nse_df.empty:
            print(f"  NSE: {len(nse_df)} records fetched")
            all_rows.append(nse_df)
        else:
            print("  NSE: 0 records — API may be unavailable or bot-blocked")

    if args.source in ("bse", "both") or not all_rows:
        print("\n── BSE Fallback ──")
        bse_df = fetch_bse_fallback(start, end)
        if not bse_df.empty:
            print(f"  BSE: {len(bse_df)} records fetched")
            all_rows.append(bse_df)
        else:
            print("  BSE: 0 records fetched")

    combined = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
    final = _merge_and_save(combined)

    print("\n── Calendar Stats ──")
    _stats(final)

    if final.empty:
        print(
            "\n  NOTE: No earnings dates fetched. run_pead_study.py will fall back to"
            "\n  price-implied large-move events (H5b proxy). To get real earnings dates,"
            "\n  re-run this script when NSE/BSE APIs are accessible."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
