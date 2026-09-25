#!/usr/bin/env python3
"""
QuantEmbrace — US EOD Downloader (Phase 1 of the US equities pivot, ADR-041)

Builds the curated US daily-bar lake from two independent free sources:

    PRIMARY   Yahoo Finance v8 chart API — split-adjusted OHLCV + adjclose
              (split+dividend adjusted) + dividend/split event lists.
    VERIFIER  Nasdaq exchange website API — split-adjusted OHLCV, independent
              origin. (Both sources use the same split-adjusted convention —
              confirmed empirically on AAPL 2005 across the 7:1 and 4:1 splits —
              so closes are directly comparable.)

Flow (the data-lake contract: LOW trust → quarantine → validate → promote):

    download  raw responses verbatim → backtest-data/quarantine/us_eod/
                  source=yahoo/{SYM}.json    source=nasdaq/{SYM}.json
                  _manifest.json             (sha256, size, url, fetched_at)
    validate  cross-source close comparison + coverage/gap/OHLC-sanity QA
              against pre-registered gates → _validation_report.json
    promote   PASS/WARN symbols only → canonical Parquet lake
                  lake/ohlcv/market=US/segment=EQ/symbol={SYM}/interval=1d/
                      year={Y}/part-0.parquet
              (schema mirrors the NSE lake + adj_close; delivery_* columns are
              null — they are an NSE microstructure concept)
    snapshot  pin the promoted files with qe.data.snapshot → ds-… id

Universe: ~23 liquid ETFs (survivorship-bias-minimal, the primary strategy
substrate) + 75 current US mega-caps (survivorship-SELECTED by construction —
single-stock strategies must address this in Phase 4; documented in the QA
report).

Usage:
    python scripts/backtest/download_us_eod.py                    # all stages
    python scripts/backtest/download_us_eod.py --stage download
    python scripts/backtest/download_us_eod.py --stage validate
    python scripts/backtest/download_us_eod.py --stage promote
    python scripts/backtest/download_us_eod.py --stage snapshot
    python scripts/backtest/download_us_eod.py --self-test        # offline

Backtest-only. No broker APIs. No live/paper trading behavior change.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import requests

# ── repo paths ────────────────────────────────────────────────────────────────
_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"

NY_TZ = "America/New_York"

# ── universe (ADR-041 Phase 1: ETFs first-class, mega-caps survivorship-caveated)
ETF_SYMBOLS = [
    "SPY", "QQQ", "DIA", "IWM",
    "XLB", "XLE", "XLF", "XLI", "XLK", "XLP", "XLU", "XLV", "XLY",
    "TLT", "IEF", "SHY", "LQD", "HYG",
    "GLD", "SLV", "EFA", "EEM", "VNQ",
]
MEGACAP_SYMBOLS = [
    "AAPL", "MSFT", "GOOGL", "AMZN", "NVDA", "META", "TSLA", "AVGO", "BRK-B",
    "JPM", "V", "MA", "UNH", "JNJ", "WMT", "XOM", "PG", "HD", "ORCL", "COST",
    "ABBV", "BAC", "KO", "CRM", "MRK", "CVX", "AMD", "PEP", "NFLX", "TMO",
    "ADBE", "LIN", "MCD", "CSCO", "ACN", "WFC", "IBM", "GE", "ABT", "TXN",
    "QCOM", "INTU", "DIS", "VZ", "CAT", "AMGN", "PFE", "PM", "MS", "AXP",
    "GS", "RTX", "NEE", "UNP", "T", "LOW", "SPGI", "HON", "BLK", "INTC",
    "UPS", "SCHW", "BA", "LMT", "BKNG", "DE", "MDT", "ADP", "TJX", "SYK",
    "BMY", "CB", "MMC", "AMAT", "PGR",
]
ALL_SYMBOLS = ETF_SYMBOLS + MEGACAP_SYMBOLS

# Nasdaq API wants exchange-style class-share symbols and an assetclass hint.
NASDAQ_SYMBOL_OVERRIDES = {"BRK-B": "BRK.B"}

# ── pre-registered validation gates ───────────────────────────────────────────
# Cross-source comparison happens in RETURNS space: |r_yahoo − r_nasdaq| per
# day. Levels cannot be compared directly — Nasdaq adjusts history for
# spin-offs/special distributions, Yahoo only for splits, so every spin-off
# symbol shows a constant pre-event level offset (confirmed empirically
# 2026-07-10: ABT/T/IBM/GE/MS/XLF… all "failed" levels by exactly their
# spin-off fraction of history). Returns are invariant to that convention;
# real corruption still fails. Level stats are kept as informational output.
XVAL_TOL = 0.0015           # abs daily-return diff; covers 2dp feed rounding
G_XVAL_PASS = 0.995         # ≥99.5% of overlapping days within tolerance
G_XVAL_WARN = 0.980         # 98–99.5% → WARN; below → FAIL
G_MIN_OVERLAP_DAYS = 250    # shorter verifier overlap → WARN (not FAIL)
# Coverage vs the union ETF calendar, measured after the symbol's first bar.
G_COV_PASS = 0.98
G_COV_WARN = 0.95
G_MAXGAP_PASS = 3           # max consecutive missing union-calendar days
G_MAXGAP_WARN = 10
# OHLC sanity violations are dropped; FAIL if they exceed this fraction.
G_OHLC_DROP_FAIL = 0.001

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}
NASDAQ_HEADERS = {**BROWSER_HEADERS,
                  "Origin": "https://www.nasdaq.com",
                  "Referer": "https://www.nasdaq.com/"}

YAHOO_URL = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{sym}"
    "?period1={p1}&period2={p2}&interval=1d&events=div%2Csplit"
)
NASDAQ_URL = (
    "https://api.nasdaq.com/api/quote/{sym}/chart"
    "?assetclass={assetclass}&fromdate={start}&todate={end}"
)

# ── Parquet canonical schema (NSE-compatible + adj_close) ─────────────────────
PARQUET_SCHEMA_COLS = [
    "timestamp", "symbol", "isin", "market", "segment", "interval",
    "open", "high", "low", "close", "adj_close", "volume",
    "prev_close", "delivery_qty", "delivery_pct", "source", "trust_level",
]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _quarantine_dir(base: Path) -> Path:
    return base / "quarantine" / "us_eod"


def _lake_dir(base: Path, symbol: str, year: int) -> Path:
    return (base / "lake" / "ohlcv" / "market=US" / "segment=EQ"
            / f"symbol={symbol}" / "interval=1d" / f"year={year}")


def _assetclass(symbol: str) -> str:
    return "etf" if symbol in ETF_SYMBOLS else "stocks"


# ── download stage ─────────────────────────────────────────────────────────────

def _fetch(session: Any, url: str, headers: dict,
           retries: int = 4, timeout: int = 30) -> Optional[bytes]:
    for attempt in range(retries):
        try:
            resp = session.get(url, headers=headers, timeout=timeout)
            if resp.status_code == 404:
                return None
            if resp.status_code == 429:
                wait = 20.0 * (attempt + 1)
                print(f"  [429] backing off {wait:.0f}s — {url.split('?')[0]}")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.content
        except Exception as e:  # requests + curl_cffi exception trees differ
            if attempt == retries - 1:
                print(f"  [ERROR] {url} — {e}")
                return None
            time.sleep(2.0 * (attempt + 1))
    print(f"  [ERROR] {url} — retries exhausted (rate-limited)")
    return None


def _yahoo_session() -> Any:
    """Yahoo hard-429s plain HTTP clients on full-history chart requests; a
    browser-TLS-impersonated session (curl_cffi, the engine behind modern
    yfinance) passes its fingerprint check. Falls back to plain requests."""
    try:
        from curl_cffi import requests as curl_requests

        session = curl_requests.Session(impersonate="chrome")
        print("  Yahoo session: curl_cffi chrome-impersonated")
        return session
    except ImportError:
        print("  Yahoo session: plain requests (curl_cffi not installed — "
              "expect 429s; pip install curl_cffi)")
        return requests.Session()


def _manifest_append(qdir: Path, entry: dict) -> None:
    manifest_file = qdir / "_manifest.json"
    existing = []
    if manifest_file.exists():
        existing = json.loads(manifest_file.read_text()).get("files", [])
    existing = [e for e in existing
                if not (e["symbol"] == entry["symbol"] and e["source"] == entry["source"])]
    existing.append(entry)
    manifest_file.write_text(json.dumps({"files": existing}, indent=2))


def download_all(base: Path, symbols: list[str], start: date, end: date,
                 rate_limit: float = 0.7, refresh: bool = False) -> dict:
    qdir = _quarantine_dir(base)
    ysession = _yahoo_session()
    nsession = requests.Session()
    p1 = int(datetime(start.year, start.month, start.day, tzinfo=timezone.utc).timestamp())
    p2 = int(datetime(end.year, end.month, end.day, tzinfo=timezone.utc).timestamp()) + 86400

    stats = {"yahoo_ok": 0, "yahoo_fail": [], "nasdaq_ok": 0, "nasdaq_missing": []}
    total = len(symbols)
    for i, sym in enumerate(symbols):
        # ── Yahoo (primary — required) ──────────────────────────────────────
        ydest = qdir / "source=yahoo" / f"{sym}.json"
        if refresh or not ydest.exists():
            yurl = YAHOO_URL.format(sym=sym, p1=p1, p2=p2)
            raw = _fetch(ysession, yurl, BROWSER_HEADERS)
            ok = False
            if raw:
                try:
                    j = json.loads(raw)
                    ok = bool(j.get("chart", {}).get("result"))
                except json.JSONDecodeError:
                    ok = False
            if ok:
                ydest.parent.mkdir(parents=True, exist_ok=True)
                ydest.write_bytes(raw)
                _manifest_append(qdir, {
                    "symbol": sym, "source": "yahoo", "sha256": _sha256_bytes(raw),
                    "size_bytes": len(raw),
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "trust_level": "LOW",
                })
                stats["yahoo_ok"] += 1
            else:
                stats["yahoo_fail"].append(sym)
                print(f"  [{i+1}/{total}] {sym}: YAHOO FAILED (primary — symbol excluded)")
                time.sleep(rate_limit)
                continue
            time.sleep(rate_limit)
        else:
            stats["yahoo_ok"] += 1

        # ── Nasdaq (verifier — best-effort) ─────────────────────────────────
        ndest = qdir / "source=nasdaq" / f"{sym}.json"
        if refresh or not ndest.exists():
            got = None
            for nsym in dict.fromkeys([NASDAQ_SYMBOL_OVERRIDES.get(sym, sym), sym]):
                url = NASDAQ_URL.format(sym=nsym, assetclass=_assetclass(sym),
                                        start=start.isoformat(), end=end.isoformat())
                raw = _fetch(nsession, url, NASDAQ_HEADERS)
                if raw:
                    try:
                        j = json.loads(raw)
                        if (j.get("data") or {}).get("chart"):
                            got = raw
                            break
                    except json.JSONDecodeError:
                        pass
                time.sleep(rate_limit)
            if got:
                ndest.parent.mkdir(parents=True, exist_ok=True)
                ndest.write_bytes(got)
                _manifest_append(qdir, {
                    "symbol": sym, "source": "nasdaq", "sha256": _sha256_bytes(got),
                    "size_bytes": len(got),
                    "fetched_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "trust_level": "LOW",
                })
                stats["nasdaq_ok"] += 1
            else:
                stats["nasdaq_missing"].append(sym)
        else:
            stats["nasdaq_ok"] += 1

        if (i + 1) % 10 == 0 or i == total - 1:
            print(f"  [{i+1}/{total}] downloaded (yahoo {stats['yahoo_ok']}, "
                  f"nasdaq {stats['nasdaq_ok']}, verifier-missing "
                  f"{len(stats['nasdaq_missing'])})")
    return stats


# ── parsers ───────────────────────────────────────────────────────────────────

def parse_yahoo(raw: bytes, start: date, end: date) -> tuple[pd.DataFrame, dict]:
    """Parse a Yahoo v8 chart JSON → (bars df indexed by NY date, events dict)."""
    j = json.loads(raw)
    result = j["chart"]["result"][0]
    ts = result.get("timestamp") or []
    quote = result["indicators"]["quote"][0]
    adj = (result["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    events = result.get("events") or {}

    idx = pd.to_datetime(ts, unit="s", utc=True).tz_convert(NY_TZ)
    df = pd.DataFrame({
        "open": quote.get("open"), "high": quote.get("high"),
        "low": quote.get("low"), "close": quote.get("close"),
        "volume": quote.get("volume"),
        "adj_close": adj if adj is not None else quote.get("close"),
    }, index=idx)
    df["ny_date"] = df.index.date
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df[(df["ny_date"] >= start) & (df["ny_date"] <= end)]
    df = df[~df["ny_date"].duplicated(keep="last")]
    df["volume"] = pd.to_numeric(df["volume"], errors="coerce").fillna(0).astype("int64")
    ev = {
        "dividends": sorted(
            (v.get("amount"), datetime.fromtimestamp(v["date"], tz=timezone.utc).date().isoformat())
            for v in (events.get("dividends") or {}).values()),
        "splits": sorted(
            (v.get("splitRatio"), datetime.fromtimestamp(v["date"], tz=timezone.utc).date().isoformat())
            for v in (events.get("splits") or {}).values()),
    }
    return df.set_index("ny_date"), ev


def parse_nasdaq(raw: bytes, start: date, end: date) -> pd.DataFrame:
    """Parse a Nasdaq chart JSON → close/volume df indexed by NY date."""
    j = json.loads(raw)
    rows = (j.get("data") or {}).get("chart") or []
    recs = []
    for r in rows:
        z = r.get("z") or {}
        try:
            d = datetime.strptime(z["dateTime"], "%m/%d/%Y").date()
            close = float(str(z["close"]).replace(",", ""))
            vol = int(str(z.get("volume", "0")).replace(",", "") or 0)
        except (KeyError, ValueError):
            continue
        if start <= d <= end:
            recs.append({"ny_date": d, "close": close, "volume": vol})
    if not recs:
        return pd.DataFrame(columns=["close", "volume"])
    df = pd.DataFrame(recs).drop_duplicates("ny_date", keep="last")
    return df.set_index("ny_date").sort_index()


# ── validate stage ────────────────────────────────────────────────────────────

def _union_calendar(base: Path, start: date, end: date) -> list[date]:
    """Trading calendar = dates where ≥50% of downloaded ETFs have a Yahoo bar."""
    qdir = _quarantine_dir(base)
    counts: dict[date, int] = {}
    n_etfs = 0
    for sym in ETF_SYMBOLS:
        f = qdir / "source=yahoo" / f"{sym}.json"
        if not f.exists():
            continue
        n_etfs += 1
        bars, _ = parse_yahoo(f.read_bytes(), start, end)
        for d in bars.index:
            counts[d] = counts.get(d, 0) + 1
    if not n_etfs:
        raise RuntimeError("no ETF downloads found — cannot build union calendar")
    return sorted(d for d, c in counts.items() if c >= max(1, n_etfs // 2))


def validate_all(base: Path, symbols: list[str], start: date, end: date) -> dict:
    qdir = _quarantine_dir(base)
    calendar = _union_calendar(base, start, end)
    cal_set = set(calendar)
    print(f"  Union ETF calendar: {len(calendar)} trading days "
          f"({calendar[0]} → {calendar[-1]})")

    report: dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "gates": {
            "xval_tol": XVAL_TOL, "xval_pass": G_XVAL_PASS, "xval_warn": G_XVAL_WARN,
            "min_overlap_days": G_MIN_OVERLAP_DAYS,
            "cov_pass": G_COV_PASS, "cov_warn": G_COV_WARN,
            "maxgap_pass": G_MAXGAP_PASS, "maxgap_warn": G_MAXGAP_WARN,
            "ohlc_drop_fail": G_OHLC_DROP_FAIL,
        },
        "union_calendar": {"n_days": len(calendar),
                           "first": calendar[0].isoformat(),
                           "last": calendar[-1].isoformat()},
        "symbols": {},
    }

    for sym in symbols:
        yfile = qdir / "source=yahoo" / f"{sym}.json"
        if not yfile.exists():
            report["symbols"][sym] = {"verdict": "FAIL", "reasons": ["no-primary-download"]}
            continue
        bars, events = parse_yahoo(yfile.read_bytes(), start, end)
        reasons: list[str] = []

        # OHLC sanity (NSE-script semantics: drop invalid rows, gate on fraction)
        valid = ((bars["low"] <= bars[["open", "close"]].min(axis=1))
                 & (bars["high"] >= bars[["open", "close"]].max(axis=1))
                 & (bars["open"] > 0) & (bars["close"] > 0))
        n_bad_ohlc = int((~valid).sum())
        drop_frac = n_bad_ohlc / max(1, len(bars))
        bars = bars[valid]

        first_d, last_d = (bars.index.min(), bars.index.max()) if len(bars) else (None, None)
        sym_days = set(bars.index)
        expected = [d for d in calendar if first_d is not None and first_d <= d <= last_d]
        n_missing = sum(1 for d in expected if d not in sym_days)
        coverage = 1 - n_missing / max(1, len(expected))
        max_gap = gap = 0
        for d in expected:
            gap = gap + 1 if d not in sym_days else 0
            max_gap = max(max_gap, gap)
        stale_days = sum(1 for d in expected[-30:] if d not in sym_days)

        # Cross-source validation (returns space — see gate comment above)
        nfile = qdir / "source=nasdaq" / f"{sym}.json"
        xval: dict[str, Any] = {"overlap_days": 0}
        if nfile.exists():
            ndf = parse_nasdaq(nfile.read_bytes(), start, end)
            joined = bars[["close"]].join(ndf[["close"]], how="inner",
                                          lsuffix="_y", rsuffix="_n").sort_index()
            if len(joined) > 1:
                r_y = joined["close_y"].pct_change().iloc[1:]
                r_n = joined["close_n"].pct_change().iloc[1:]
                diff = (r_y - r_n).abs()
                bad = diff[diff > XVAL_TOL]
                level = (joined["close_y"] - joined["close_n"]).abs() / joined["close_y"]
                xval = {
                    "overlap_days": int(len(joined)),
                    "overlap_first": joined.index.min().isoformat(),
                    "overlap_last": joined.index.max().isoformat(),
                    "frac_within_tol": round(1 - len(bad) / len(diff), 6),
                    "median_ret_diff": round(float(diff.median()), 8),
                    "max_ret_diff": round(float(diff.max()), 6),
                    "n_bad_days": int(len(bad)),
                    "worst_days": [d.isoformat() for d in
                                   diff.nlargest(min(3, len(bad))).index] if len(bad) else [],
                    # informational: level offset ⇒ adjustment-convention gap
                    "level_median_rel_diff": round(float(level.median()), 8),
                    "level_max_rel_diff": round(float(level.max()), 6),
                    "adjustment_convention_gap": bool(level.median() > XVAL_TOL),
                }

        # ── verdict against pre-registered gates ────────────────────────────
        verdict = "PASS"
        if drop_frac > G_OHLC_DROP_FAIL:
            verdict, reasons = "FAIL", reasons + [f"ohlc-drop-frac={drop_frac:.4f}"]
        if not len(bars):
            verdict, reasons = "FAIL", reasons + ["no-valid-bars"]
        else:
            fw = xval.get("frac_within_tol")
            if xval["overlap_days"] == 0:
                verdict = "WARN" if verdict == "PASS" else verdict
                reasons.append("no-verifier-data")
            elif xval["overlap_days"] < G_MIN_OVERLAP_DAYS:
                verdict = "WARN" if verdict == "PASS" else verdict
                reasons.append(f"verifier-overlap-short={xval['overlap_days']}d")
            elif fw is not None and fw < G_XVAL_WARN:
                verdict, reasons = "FAIL", reasons + [f"xval-frac={fw:.4f}<{G_XVAL_WARN}"]
            elif fw is not None and fw < G_XVAL_PASS:
                verdict = "WARN" if verdict == "PASS" else verdict
                reasons.append(f"xval-frac={fw:.4f}")
            if coverage < G_COV_WARN:
                verdict, reasons = "FAIL", reasons + [f"coverage={coverage:.4f}<{G_COV_WARN}"]
            elif coverage < G_COV_PASS:
                verdict = "WARN" if verdict == "PASS" else verdict
                reasons.append(f"coverage={coverage:.4f}")
            if max_gap > G_MAXGAP_WARN:
                verdict, reasons = "FAIL", reasons + [f"max-gap={max_gap}>{G_MAXGAP_WARN}"]
            elif max_gap > G_MAXGAP_PASS:
                verdict = "WARN" if verdict == "PASS" else verdict
                reasons.append(f"max-gap={max_gap}")
            if stale_days:
                # symbol stopped trading before the frontier (delisting/rename)
                verdict = "WARN" if verdict == "PASS" else verdict
                reasons.append(f"stale-frontier={stale_days}d-missing-of-last-30")

        report["symbols"][sym] = {
            "verdict": verdict, "reasons": reasons,
            "type": "etf" if sym in ETF_SYMBOLS else "stock",
            "n_rows": int(len(bars)),
            "first_date": first_d.isoformat() if first_d else None,
            "last_date": last_d.isoformat() if last_d else None,
            "coverage": round(coverage, 6), "n_missing": n_missing, "max_gap": max_gap,
            "n_bad_ohlc_dropped": n_bad_ohlc,
            "n_dividends": len(events["dividends"]), "n_splits": len(events["splits"]),
            "xval": xval,
        }

    verdicts = [s["verdict"] for s in report["symbols"].values()]
    report["summary"] = {
        "pass": verdicts.count("PASS"), "warn": verdicts.count("WARN"),
        "fail": verdicts.count("FAIL"),
        "excluded": sorted(s for s, v in report["symbols"].items() if v["verdict"] == "FAIL"),
    }
    out = qdir / "_validation_report.json"
    out.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"  Verdicts: {report['summary']['pass']} PASS, {report['summary']['warn']} WARN, "
          f"{report['summary']['fail']} FAIL → {out}")
    return report


# ── promote stage ─────────────────────────────────────────────────────────────

def promote_all(base: Path, start: date, end: date) -> dict:
    qdir = _quarantine_dir(base)
    report_file = qdir / "_validation_report.json"
    if not report_file.exists():
        raise RuntimeError("run --stage validate first (no _validation_report.json)")
    report = json.loads(report_file.read_text())

    promoted, skipped = [], []
    files_written = 0
    for sym, info in sorted(report["symbols"].items()):
        if info["verdict"] == "FAIL":
            skipped.append(sym)
            continue
        bars, _ = parse_yahoo((qdir / "source=yahoo" / f"{sym}.json").read_bytes(),
                              start, end)
        valid = ((bars["low"] <= bars[["open", "close"]].min(axis=1))
                 & (bars["high"] >= bars[["open", "close"]].max(axis=1))
                 & (bars["open"] > 0) & (bars["close"] > 0))
        bars = bars[valid].sort_index()

        # Daily bar closes at 16:00 America/New_York (honest close timestamp;
        # DST resolved per-date by the tz database).
        df = bars.reset_index()
        df["timestamp"] = pd.to_datetime(df["ny_date"].astype(str) + " 16:00:00").dt.tz_localize(
            NY_TZ, nonexistent="shift_forward", ambiguous=True)
        df["symbol"] = sym
        df["isin"] = None
        df["market"] = "US"
        df["segment"] = "EQ"
        df["interval"] = "1d"
        df["prev_close"] = df["close"].shift(1)
        df["delivery_qty"] = None
        df["delivery_pct"] = None
        df["source"] = "yahoo"
        df["trust_level"] = "MEDIUM"  # free source, cross-validated vs nasdaq
        df = df[PARQUET_SCHEMA_COLS]

        for year, grp in df.groupby(df["timestamp"].dt.year):
            lake_dir = _lake_dir(base, sym, int(year))
            lake_dir.mkdir(parents=True, exist_ok=True)
            table = pa.Table.from_pandas(grp.reset_index(drop=True), preserve_index=False)
            pq.write_table(table, lake_dir / "part-0.parquet", compression="snappy")
            files_written += 1
        promoted.append(sym)

    universe_ref = base / "reference" / "us_universe.json"
    universe_ref.parent.mkdir(parents=True, exist_ok=True)
    universe_ref.write_text(json.dumps({
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "range": {"start": start.isoformat(), "end": end.isoformat()},
        "promoted": promoted, "excluded": skipped,
        "symbols": {s: {"type": report["symbols"][s]["type"],
                        "verdict": report["symbols"][s]["verdict"],
                        "first_date": report["symbols"][s].get("first_date"),
                        "last_date": report["symbols"][s].get("last_date")}
                    for s in promoted},
    }, indent=2, sort_keys=True))

    print(f"  Promoted {len(promoted)} symbols ({files_written} parquet files); "
          f"excluded {len(skipped)}: {', '.join(skipped) or '—'}")
    print(f"  Universe reference → {universe_ref}")
    return {"promoted": promoted, "excluded": skipped, "files": files_written}


# ── snapshot stage ────────────────────────────────────────────────────────────

def snapshot_us(base: Path, start: date, end: date) -> str:
    sys.path.insert(0, str(_REPO))
    from qe.data.panel import load_panel, resolve_panel_files
    from qe.data.snapshot import create_snapshot

    lake_root = base / "lake"
    files = resolve_panel_files(lake_root, start, end, market="US")
    manifest = create_snapshot(lake_root, files, scope={
        "market": "US", "segment": "EQ", "interval": "1d",
        "start": start.isoformat(), "end": end.isoformat(),
    })
    panel = load_panel(files, start, end, tz=NY_TZ)
    print(f"  Snapshot {manifest['snapshot_id']} pins {manifest['n_files']} files")
    print(f"  Panel smoke test: {panel.close.shape[0]} days x {panel.close.shape[1]} symbols, "
          f"{panel.date_at(0)} → {panel.date_at(len(panel.index) - 1)}")
    return manifest["snapshot_id"]


# ── self-test (offline, synthetic fixtures) ───────────────────────────────────

def _synthetic_yahoo(sym: str, days: list[date], px0: float,
                     drop_days: set[date] = frozenset()) -> bytes:
    ts, o, h, l, c, v, adj = [], [], [], [], [], [], []
    px = px0
    for i, d in enumerate(days):
        if d in drop_days:
            continue
        px *= 1 + (0.001 if i % 2 == 0 else -0.0005)
        ts.append(int(datetime(d.year, d.month, d.day, 14, 30,
                               tzinfo=timezone.utc).timestamp()))
        o.append(px * 0.999); h.append(px * 1.005); l.append(px * 0.995)
        c.append(px); v.append(1_000_000 + i); adj.append(px * 0.98)
    return json.dumps({"chart": {"result": [{
        "timestamp": ts,
        "indicators": {"quote": [{"open": o, "high": h, "low": l, "close": c, "volume": v}],
                       "adjclose": [{"adjclose": adj}]},
        "events": {"dividends": {"1": {"amount": 0.5, "date": ts[0] if ts else 0}}},
    }]}}).encode()


def _synthetic_nasdaq_from_yahoo(yraw: bytes, corrupt_frac: float = 0.0,
                                 spinoff_at: float = 0.0) -> bytes:
    """corrupt_frac: per-day alternating ±1% noise on that leading fraction of
    days (breaks returns on every affected day → must FAIL).
    spinoff_at: constant 0.9x level scaling of the first fraction of history
    (a spin-off adjustment-convention gap → returns still match → must PASS)."""
    j = json.loads(yraw)
    r = j["chart"]["result"][0]
    rows = []
    n = len(r["timestamp"])
    for i, (t, c, v) in enumerate(zip(r["timestamp"],
                                      r["indicators"]["quote"][0]["close"],
                                      r["indicators"]["quote"][0]["volume"])):
        d = datetime.fromtimestamp(t, tz=timezone.utc).date()
        px = c
        if corrupt_frac and i < int(n * corrupt_frac):
            px *= 1.01 if i % 2 == 0 else 0.99
        if spinoff_at and i < int(n * spinoff_at):
            px *= 0.9
        rows.append({"z": {"dateTime": d.strftime("%m/%d/%Y"), "close": f"{px:.4f}",
                           "open": "1", "high": "1", "low": "1", "volume": f"{v:,}"}})
    return json.dumps({"data": {"chart": rows}}).encode()


def self_test() -> int:
    import tempfile
    print("Self-test (offline, synthetic fixtures)...")
    days = [d for d in (date(2024, 1, 1) + timedelta(n) for n in range(380))
            if d.weekday() < 5]
    start, end = days[0], days[-1]

    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        qdir = _quarantine_dir(base)
        # ETFs (calendar builders) must exist; give SPY-like clean data.
        cases = {}
        for sym in ETF_SYMBOLS:
            cases[sym] = _synthetic_yahoo(sym, days, 400.0)
        cases["AAPL"] = _synthetic_yahoo("AAPL", days, 180.0)               # clean PASS
        gap_days = set(days[10:16])                                        # 6-day gap → WARN
        cases["MSFT"] = _synthetic_yahoo("MSFT", days, 400.0, drop_days=gap_days)
        cases["NVDA"] = _synthetic_yahoo("NVDA", days, 100.0)               # noisy xval → FAIL
        cases["ABT"] = _synthetic_yahoo("ABT", days, 110.0)                 # spin-off gap → PASS

        for sym, raw in cases.items():
            (qdir / "source=yahoo").mkdir(parents=True, exist_ok=True)
            (qdir / "source=yahoo" / f"{sym}.json").write_bytes(raw)
            (qdir / "source=nasdaq").mkdir(parents=True, exist_ok=True)
            corrupt = 0.10 if sym == "NVDA" else 0.0
            spin = 0.5 if sym == "ABT" else 0.0
            (qdir / "source=nasdaq" / f"{sym}.json").write_bytes(
                _synthetic_nasdaq_from_yahoo(raw, corrupt_frac=corrupt, spinoff_at=spin))

        syms = ETF_SYMBOLS + ["AAPL", "MSFT", "NVDA", "ABT"]
        report = validate_all(base, syms, start, end)
        assert report["symbols"]["AAPL"]["verdict"] == "PASS", report["symbols"]["AAPL"]
        assert report["symbols"]["MSFT"]["verdict"] == "WARN", report["symbols"]["MSFT"]
        assert report["symbols"]["NVDA"]["verdict"] == "FAIL", report["symbols"]["NVDA"]
        assert "NVDA" in report["summary"]["excluded"]
        # Adjustment-convention gap (constant pre-event level offset) must NOT
        # fail returns-space validation, and must be flagged as informational.
        assert report["symbols"]["ABT"]["verdict"] == "PASS", report["symbols"]["ABT"]
        assert report["symbols"]["ABT"]["xval"]["adjustment_convention_gap"] is True

        result = promote_all(base, start, end)
        assert "AAPL" in result["promoted"] and "MSFT" in result["promoted"]
        assert "NVDA" in result["excluded"]

        # Promoted parquet is loadable and NY-tz-stamped
        f = _lake_dir(base, "AAPL", 2024) / "part-0.parquet"
        df = pd.read_parquet(f)
        n_2024 = sum(1 for d in days if d.year == 2024)
        assert len(df) == n_2024, (len(df), n_2024)
        assert str(df["timestamp"].dt.tz) == NY_TZ
        assert df["market"].eq("US").all() and df["trust_level"].eq("MEDIUM").all()
        assert (df["adj_close"] < df["close"]).all()  # synthetic adj = 0.98x

        # Snapshot + panel smoke test on the synthetic lake
        sid = snapshot_us(base, start, end)
        assert sid.startswith("ds-")

    print("Self-test PASS ✅ (PASS/WARN/FAIL verdicts, promotion filter, "
          "NY-tz parquet, snapshot+panel)")
    return 0


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="US EOD lake builder (ADR-041 Phase 1)")
    p.add_argument("--stage", choices=["download", "validate", "promote", "snapshot", "all"],
                   default="all")
    p.add_argument("--start", default="2005-01-01")
    p.add_argument("--end", default=date.today().isoformat())
    p.add_argument("--base", default=str(_DEFAULT_BASE))
    p.add_argument("--symbols", default=None,
                   help="Comma-separated subset (default: full ETF+megacap universe)")
    p.add_argument("--rate-limit", type=float, default=0.7)
    p.add_argument("--refresh", action="store_true",
                   help="Re-download even if quarantine files exist")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args()

    if args.self_test:
        return self_test()

    base = Path(args.base)
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    symbols = args.symbols.split(",") if args.symbols else ALL_SYMBOLS

    if args.stage in ("download", "all"):
        print(f"── Downloading {len(symbols)} symbols ({start} → {end}) ──")
        stats = download_all(base, symbols, start, end,
                             rate_limit=args.rate_limit, refresh=args.refresh)
        if stats["yahoo_fail"]:
            print(f"  [WARN] primary failed for: {', '.join(stats['yahoo_fail'])}")
    if args.stage in ("validate", "all"):
        print("── Validating (cross-source + coverage gates) ──")
        validate_all(base, symbols, start, end)
    if args.stage in ("promote", "all"):
        print("── Promoting PASS/WARN symbols to lake ──")
        promote_all(base, start, end)
    if args.stage in ("snapshot", "all"):
        print("── Creating pinned data snapshot ──")
        snapshot_us(base, start, end)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
