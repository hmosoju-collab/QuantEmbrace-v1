#!/usr/bin/env python3
"""
QuantEmbrace — NSE F&O Bhavcopy downloader (NIFTY option chains, EOD, Options/Vol O-2).

Downloads the NSE **derivatives (F&O) bhavcopy** — EOD one row per contract per day,
including ALL expired NIFTY index-option strikes/expiries — and normalises NIFTY
options into a local Parquet chain lake the O-2 backtester reads. This is the FREE
path to 3+ yr of NIFTY option history (Zerodha/Kite cannot backfill expired option
tokens — they are purged from the instrument master).

Reuses the equity downloader's NSE bot-shield session (same archive family).

TWO NSE FORMATS (handled, with the 2024-07 transition)
------------------------------------------------------
  • Legacy (pre ~2024-07-08):
      https://nsearchives.nseindia.com/content/historical/DERIVATIVES/{YYYY}/{MMM}/fo{DDMMMYYYY}bhav.csv.zip
      cols: INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,
            CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP   (NIFTY options: INSTRUMENT=OPTIDX, SYMBOL=NIFTY)
  • UDiFF (from ~2024-07-08):
      https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{YYYYMMDD}_F_0000.csv.zip
      cols: TradDt,...,FinInstrmTp,TckrSymb,XpryDt,StrkPric,OptnTp,...,ClsPric,SttlmPric,UndrlygPric,
            OpnIntrst,TtlTradgVol,...  (NIFTY index options: FinInstrmTp=IDO, TckrSymb=NIFTY)
NSE occasionally tweaks archive paths; this tries both URLs per date and reports clearly.

RUN THIS LOCALLY (NSE archives need a real browser session; the sandbox cannot reach them)
-----------------------------------------------------------------------------------------
    pip install requests pandas pyarrow
    python scripts/backtest/download_fo_bhavcopy.py --start 2022-06-01 --end 2025-06-30
Idempotent — re-run to resume (skips dates already written, unless --force).

Output (read by run_options_vol_backtest.py --chain):
    backtest-data/lake/options/underlying=NIFTY/date={YYYY-MM-DD}/part-0.parquet
Canonical cols: trade_date, expiry, strike, opt_type, open, high, low, close, settle, oi, volume, underlying.

Backtest-only. NSE official EOD (HIGH trust). No broker APIs, no live/paper trading, no capital changes.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import zipfile
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
# reuse the equity downloader's bot-shield session + helpers (no rewrite)
from download_bhavcopy import _build_session, _trading_days, _sha256_bytes, BROWSER_HEADERS  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"

SOURCE_NAME = "nse_fo_bhavcopy"
TRUST_LEVEL = "HIGH"
LICENSE = "nse-public-archive"
UNDERLYING = "NIFTY"

_MONTH_ABBR = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
FO_UDIFF_CUTOFF = date(2024, 7, 8)   # UDiFF F&O bhavcopy go-live

OPT_SCHEMA = pa.schema([
    ("trade_date", pa.date32()), ("expiry", pa.date32()),
    ("strike", pa.float64()), ("opt_type", pa.string()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
    ("close", pa.float64()), ("settle", pa.float64()),
    ("oi", pa.int64()), ("volume", pa.int64()), ("underlying", pa.float64()),
])


# ── URLs ───────────────────────────────────────────────────────────────────────
def _legacy_url(d: date) -> str:
    return ("https://nsearchives.nseindia.com/content/historical/DERIVATIVES/"
            f"{d.year}/{_MONTH_ABBR[d.month - 1]}/"
            f"fo{d.day:02d}{_MONTH_ABBR[d.month - 1]}{d.year}bhav.csv.zip")


def _udiff_url(d: date) -> str:
    return ("https://nsearchives.nseindia.com/content/fo/"
            f"BhavCopy_NSE_FO_0_0_0_{d.strftime('%Y%m%d')}_F_0000.csv.zip")


def _candidate_urls(d: date) -> list[str]:
    # Prefer the era-appropriate URL, but try the other as a fallback across the transition.
    return [_udiff_url(d), _legacy_url(d)] if d >= FO_UDIFF_CUTOFF else [_legacy_url(d), _udiff_url(d)]


# ── parsing ────────────────────────────────────────────────────────────────────
def _norm_cols(df: pd.DataFrame) -> dict:
    return {c.strip().upper(): c for c in df.columns}


def _parse_legacy(df: pd.DataFrame, td: date) -> pd.DataFrame:
    c = _norm_cols(df)
    need = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "STRIKE_PR", "OPTION_TYP", "CLOSE"]
    if not all(k in c for k in need):
        return pd.DataFrame()
    m = df[(df[c["INSTRUMENT"]].astype(str).str.upper() == "OPTIDX") &
           (df[c["SYMBOL"]].astype(str).str.upper() == UNDERLYING)].copy()
    if m.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "trade_date": td,
        "expiry": pd.to_datetime(m[c["EXPIRY_DT"]], errors="coerce").dt.date,
        "strike": pd.to_numeric(m[c["STRIKE_PR"]], errors="coerce"),
        "opt_type": m[c["OPTION_TYP"]].astype(str).str.upper().str.strip(),
        "open": pd.to_numeric(m.get(c.get("OPEN"), pd.NA), errors="coerce"),
        "high": pd.to_numeric(m.get(c.get("HIGH"), pd.NA), errors="coerce"),
        "low": pd.to_numeric(m.get(c.get("LOW"), pd.NA), errors="coerce"),
        "close": pd.to_numeric(m[c["CLOSE"]], errors="coerce"),
        "settle": pd.to_numeric(m.get(c.get("SETTLE_PR"), m[c["CLOSE"]]), errors="coerce"),
        "oi": pd.to_numeric(m.get(c.get("OPEN_INT"), 0), errors="coerce"),
        "volume": pd.to_numeric(m.get(c.get("CONTRACTS"), 0), errors="coerce"),
        "underlying": pd.NA,
    })
    return out


def _parse_udiff(df: pd.DataFrame, td: date) -> pd.DataFrame:
    c = _norm_cols(df)
    need = ["FININSTRMTP", "TCKRSYMB", "XPRYDT", "STRKPRIC", "OPTNTP", "CLSPRIC"]
    if not all(k in c for k in need):
        return pd.DataFrame()
    tp = df[c["FININSTRMTP"]].astype(str).str.upper()
    m = df[(tp == "IDO") & (df[c["TCKRSYMB"]].astype(str).str.upper() == UNDERLYING)].copy()
    if m.empty:
        return pd.DataFrame()
    out = pd.DataFrame({
        "trade_date": td,
        "expiry": pd.to_datetime(m[c["XPRYDT"]], errors="coerce").dt.date,
        "strike": pd.to_numeric(m[c["STRKPRIC"]], errors="coerce"),
        "opt_type": m[c["OPTNTP"]].astype(str).str.upper().str.strip(),
        "open": pd.to_numeric(m.get(c.get("OPNPRIC"), pd.NA), errors="coerce"),
        "high": pd.to_numeric(m.get(c.get("HGHPRIC"), pd.NA), errors="coerce"),
        "low": pd.to_numeric(m.get(c.get("LWPRIC"), pd.NA), errors="coerce"),
        "close": pd.to_numeric(m[c["CLSPRIC"]], errors="coerce"),
        "settle": pd.to_numeric(m.get(c.get("STTLMPRIC"), m[c["CLSPRIC"]]), errors="coerce"),
        "oi": pd.to_numeric(m.get(c.get("OPNINTRST"), 0), errors="coerce"),
        "volume": pd.to_numeric(m.get(c.get("TTLTRADGVOL"), 0), errors="coerce"),
        "underlying": pd.to_numeric(m.get(c.get("UNDRLYGPRIC"), pd.NA), errors="coerce"),
    })
    return out


def _parse_zip_bytes(raw: bytes, td: date) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        name = z.namelist()[0]
        df = pd.read_csv(io.BytesIO(z.read(name)))
    # try UDiFF first then legacy (parser self-detects by columns)
    out = _parse_udiff(df, td)
    if out.empty:
        out = _parse_legacy(df, td)
    out = out.dropna(subset=["expiry", "strike", "opt_type", "close"])
    out = out[out["opt_type"].isin(["CE", "PE"])]
    for col in ("oi", "volume"):
        out[col] = out[col].fillna(0).astype("int64")
    return out.reset_index(drop=True)


# ── lake IO ────────────────────────────────────────────────────────────────────
def _date_dir(base: Path, d: date) -> Path:
    return base / "lake" / "options" / f"underlying={UNDERLYING}" / f"date={d.isoformat()}"


def _write_day(base: Path, d: date, df: pd.DataFrame) -> int:
    if df.empty:
        return 0
    out_dir = _date_dir(base, d)
    out_dir.mkdir(parents=True, exist_ok=True)
    table = pa.Table.from_pandas(df[[f.name for f in OPT_SCHEMA]], schema=OPT_SCHEMA, preserve_index=False)
    pq.write_table(table, out_dir / "part-0.parquet", compression="snappy")
    return len(df)


# status codes: >0 rows written · -1 skip(exists) · -2 no-file/holiday(404 on all URLs) · 0 real failure
def _download_day(session, d: date, base: Path, force: bool, verbose: bool) -> int:
    out_path = _date_dir(base, d) / "part-0.parquet"
    if out_path.exists() and not force:
        if verbose:
            print(f"    skip {d} (exists)")
        return -1
    last_err = ""
    all_404 = True
    for url in _candidate_urls(d):
        try:
            resp = session.get(url, timeout=30, headers=BROWSER_HEADERS)
            if resp.status_code == 200 and resp.content[:2] == b"PK":
                df = _parse_zip_bytes(resp.content, d)
                n = _write_day(base, d, df)
                if verbose:
                    print(f"    {d}: {n} NIFTY option rows  [{url.rsplit('/', 1)[-1]}]")
                return n
            last_err = f"HTTP {resp.status_code}"
            if resp.status_code != 404:
                all_404 = False
        except Exception as exc:
            last_err = str(exc); all_404 = False
        time.sleep(0.3)
    if all_404:
        return -2   # expected for NSE holidays/weekends — no bhavcopy published; stay quiet
    print(f"    {d}: FAILED ({last_err})", file=sys.stderr)
    return 0


# ── self-test (offline; synthetic bytes for BOTH formats) ──────────────────────
def _zip_csv(df: pd.DataFrame, name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, df.to_csv(index=False))
    return buf.getvalue()


def _self_test() -> int:
    print("SELF-TEST: F&O bhavcopy parsing (both formats)...")
    td = date(2023, 1, 25)

    legacy = pd.DataFrame({
        "INSTRUMENT": ["OPTIDX", "OPTIDX", "FUTIDX", "OPTSTK"],
        "SYMBOL": ["NIFTY", "NIFTY", "NIFTY", "RELIANCE"],
        "EXPIRY_DT": ["25-Jan-2023"] * 4,
        "STRIKE_PR": [18000, 18000, 0, 2500],
        "OPTION_TYP": ["CE", "PE", "XX", "CE"],
        "OPEN": [120, 110, 0, 50], "HIGH": [130, 120, 0, 55], "LOW": [100, 95, 0, 45],
        "CLOSE": [115, 105, 18050, 52], "SETTLE_PR": [115, 105, 18050, 52],
        "CONTRACTS": [1000, 900, 50, 10], "OPEN_INT": [50000, 48000, 1000, 200],
    })
    out = _parse_zip_bytes(_zip_csv(legacy, "fo25JAN2023bhav.csv"), td)
    assert len(out) == 2, f"legacy: expected 2 NIFTY options, got {len(out)}"
    assert set(out["opt_type"]) == {"CE", "PE"} and (out["strike"] == 18000).all()
    print(f"  legacy: {len(out)} NIFTY option rows parsed (FUTIDX + non-NIFTY filtered out) ✅")

    udiff = pd.DataFrame({
        "TradDt": ["2024-09-26"] * 3, "FinInstrmTp": ["IDO", "IDO", "IDF"],
        "TckrSymb": ["NIFTY", "NIFTY", "NIFTY"], "XpryDt": ["2024-09-26"] * 3,
        "StrkPric": [25000, 25000, 0], "OptnTp": ["CE", "PE", ""],
        "OpnPric": [80, 70, 0], "HghPric": [90, 80, 0], "LwPric": [60, 55, 0],
        "ClsPric": [75, 65, 25100], "SttlmPric": [75, 65, 25100],
        "UndrlygPric": [25080, 25080, 25080], "OpnIntrst": [60000, 55000, 2000],
        "TtlTradgVol": [1200, 1100, 80],
    })
    out2 = _parse_zip_bytes(_zip_csv(udiff, "BhavCopy_NSE_FO_0_0_0_20240926_F_0000.csv"), date(2024, 9, 26))
    assert len(out2) == 2, f"udiff: expected 2 NIFTY options, got {len(out2)}"
    assert (out2["underlying"] == 25080).all(), "udiff underlying must be parsed"
    print(f"  UDiFF: {len(out2)} NIFTY option rows parsed, underlying captured ✅")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        n = _write_day(base, td, out)
        p = _date_dir(base, td) / "part-0.parquet"
        assert p.exists() and len(pd.read_parquet(p)) == n
    print("  lake write/read round-trip ✅")
    print("SELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Download NSE F&O bhavcopy → NIFTY option chain lake")
    ap.add_argument("--start", default="2022-06-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--base", default=str(_DEFAULT_BASE))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()

    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    base = Path(args.base)
    days = _trading_days(start, end)

    print("=" * 72)
    print("QuantEmbrace — NSE F&O Bhavcopy → NIFTY option chains (O-2). Backtest-only.")
    print("=" * 72)
    print(f"  Period   : {start} → {end} ({len(days)} trading days)")
    print(f"  Lake     : {base / 'lake' / 'options' / f'underlying={UNDERLYING}'}")
    print(f"  Source   : {SOURCE_NAME} (trust={TRUST_LEVEL})")
    print()

    session = _build_session()
    total_rows = total_days = 0
    n_skip = n_holiday = n_fail = 0
    manifest = []
    for i, d in enumerate(days, 1):
        n = _download_day(session, d, base, args.force, args.verbose)
        if n == -1:
            n_skip += 1
        elif n == -2:
            n_holiday += 1
        elif n > 0:
            total_rows += n; total_days += 1
            manifest.append({"date": d.isoformat(), "rows": n})
        else:
            n_fail += 1
        if i % 50 == 0 or i == len(days):
            print(f"  …{i}/{len(days)} days · {total_days} downloaded ({total_rows:,} rows) · "
                  f"{n_skip} already-have · {n_holiday} holidays · {n_fail} failed")
        if i % 200 == 0:
            session = _build_session()  # re-prime cookies

    mdir = base / "raw" / "fo_bhavcopy"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "_manifest.json").write_text(json.dumps({
        "source": SOURCE_NAME, "trust_level": TRUST_LEVEL, "license": LICENSE,
        "underlying": UNDERLYING, "days": total_days, "rows": total_rows,
        "fetched_at": datetime.now().isoformat(), "entries": manifest,
    }, indent=2))

    print(f"\nDone: {total_rows:,} NIFTY option rows across {total_days} days "
          f"({n_skip} already-have, {n_holiday} holidays/no-file, {n_fail} real failures).")
    if n_fail:
        print(f"  ⚠️ {n_fail} dates failed for non-404 reasons — re-run to retry (idempotent), "
              f"or paste a failing date and I'll check the URL.")
    print(f"Lake: {base / 'lake' / 'options' / f'underlying={UNDERLYING}'}")
    print("Next:  python scripts/backtest/run_options_vol_backtest.py --chain backtest-data/lake/options")
    print("Backtest-only. Advisory. No live trading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
