#!/usr/bin/env python3
"""Download NSE corporate announcements into the immutable RAW zone (ADR-043 P9).

This script is the ONLY network component of the AI news pipeline (qe.ai itself
has no network code). It stores what NSE returned, verbatim, one wrapper per
record, and interprets nothing:

    backtest-data/raw/nse_announcements/ingest=<UTC stamp>/announcements.jsonl
    {"fetched_at": "<tz-aware ISO>", "source": "NSE_ANNOUNCEMENTS", "endpoint": "...", "record": {...raw...}}

Sanitising, injection screening, point-in-time validation, dedupe and promotion
are done by `python -m qe.ai corpus ingest` (tested, quarantine-first). A raw
batch is never modified after it is written (opened with mode "x").

Usage:
    python scripts/backtest/download_nse_announcements.py --from 2026-08-01 --to 2026-09-25
    python scripts/backtest/download_nse_announcements.py --from 2026-09-01 --to 2026-09-25 --symbols INFY,TCS
    python -m qe.ai corpus ingest        # then curate

NSE rate-limits and blocks bots: the session is primed like the other NSE
downloaders, requests are spaced (default 1.5 s), and failures are reported, not
retried aggressively. Backtest/research only. No broker. Advisory.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import sys
import time

_REPO = Path(__file__).resolve().parents[2]
RAW_DIR = _REPO / "backtest-data" / "raw" / "nse_announcements"
NSE_HOME = "https://www.nseindia.com"
ENDPOINT = (
    "https://www.nseindia.com/api/corporate-announcements"
    "?index=equities&from_date={from_d}&to_date={to_d}"
)
SYMBOL_ENDPOINT = ENDPOINT + "&symbol={symbol}"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-announcements",
}
MAX_WINDOW_DAYS = 31

FetchFn = Callable[[str], list[dict]]


def _requests_fetcher(pause: float) -> FetchFn:
    import requests  # imported lazily: only a real download needs it

    session = requests.Session()
    session.headers.update(HEADERS)
    try:
        session.get(NSE_HOME, timeout=8)  # prime cookies
        time.sleep(1.5)
    except Exception as exc:  # noqa: BLE001 - priming failure is reported, then we try anyway
        print(f"  WARN: session priming failed ({type(exc).__name__}); continuing")

    def fetch(url: str) -> list[dict]:
        r = session.get(url, timeout=20)
        if r.status_code == 403:
            time.sleep(3)
            session.get(NSE_HOME, timeout=8)
            r = session.get(url, timeout=20)
        r.raise_for_status()
        time.sleep(pause)
        data = r.json()
        return data if isinstance(data, list) else []

    return fetch


def windows(start: date, end: date) -> Iterable[tuple[date, date]]:
    cur = start
    while cur <= end:
        stop = min(cur + timedelta(days=MAX_WINDOW_DAYS - 1), end)
        yield cur, stop
        cur = stop + timedelta(days=1)


def download(
    start: date,
    end: date,
    *,
    symbols: list[str] | None = None,
    fetch: FetchFn,
    out_dir: Path = RAW_DIR,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> tuple[Path, int, int]:
    """Write one immutable raw batch; return (path, n_records, n_failed_requests)."""
    if end < start:
        raise ValueError("--to is before --from")
    stamp = now().strftime("%Y%m%dT%H%M%S%fZ")
    batch = out_dir / f"ingest={stamp}"
    batch.mkdir(parents=True, exist_ok=False)
    path = batch / "announcements.jsonl"
    n = failed = 0
    with open(path, "x", encoding="utf-8") as fh:
        for lo, hi in windows(start, end):
            targets = symbols or [None]
            for sym in targets:
                fmt = {"from_d": lo.strftime("%d-%m-%Y"), "to_d": hi.strftime("%d-%m-%Y")}
                url = (
                    SYMBOL_ENDPOINT.format(symbol=sym.strip().upper(), **fmt)
                    if sym
                    else ENDPOINT.format(**fmt)
                )
                try:
                    records = fetch(url)
                except Exception as exc:  # noqa: BLE001 - reported, never swallowed silently
                    failed += 1
                    print(f"  FAILED {lo}..{hi} {sym or '*'}: {type(exc).__name__}")
                    continue
                fetched_at = now().isoformat(timespec="seconds")
                for rec in records:
                    fh.write(
                        json.dumps(
                            {
                                "fetched_at": fetched_at,
                                "source": "NSE_ANNOUNCEMENTS",
                                "endpoint": url.split("?")[0],
                                "record": rec,
                            },
                            sort_keys=True,
                        )
                        + "\n"
                    )
                    n += 1
                print(f"  {lo}..{hi} {sym or '*'}: {len(records)} record(s)")
    return path, n, failed


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Download NSE announcements to the raw zone")
    ap.add_argument("--from", dest="start", type=date.fromisoformat, required=True)
    ap.add_argument("--to", dest="end", type=date.fromisoformat, required=True)
    ap.add_argument("--symbols", default="", help="comma-separated NSE symbols (default: market-wide)")
    ap.add_argument("--pause", type=float, default=1.5, help="seconds between requests")
    args = ap.parse_args(argv)
    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()] or None
    path, n, failed = download(args.start, args.end, symbols=symbols, fetch=_requests_fetcher(args.pause))
    print(f"raw batch: {path}\nrecords  : {n}   failed requests: {failed}")
    print("next     : python -m qe.ai corpus ingest   (sanitise -> screen -> validate -> promote)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
