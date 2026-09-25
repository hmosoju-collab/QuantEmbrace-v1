#!/usr/bin/env python3
"""
QuantEmbrace — NSE index-FUTURES bhavcopy downloader (F1-full, Options/Futures program).

Companion to `download_fo_bhavcopy.py` (which captures OPTIONS). The daily NSE F&O zip also
contains the FUTURES (FUTIDX) rows; this pulls NIFTY/BANKNIFTY index futures (all expiries,
EOD OHLC + settle + OI) into a futures lake the F1-full backtest reads. Separate script so the
tested options downloader is untouched; it re-fetches the same daily zips but extracts futures.

Why this (not Kite): Kite purges expired futures instrument tokens just like options — it cannot
backfill 3 yr of expired NIFTY futures contracts. The free NSE F&O bhavcopy can (all expiries, EOD).

RUN LOCALLY (NSE archives need a browser session; sandbox has no egress):
    python scripts/backtest/download_fo_futures.py --start 2022-06-01 --end 2025-06-30
Idempotent — skips dates already written. Output (read by run_overnight_futures_study.py --futures):
    backtest-data/lake/futures/underlying={NIFTY,BANKNIFTY}/date={YYYY-MM-DD}/part-0.parquet
    cols: trade_date, expiry, underlying, open, high, low, close, settle, oi, volume

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
from download_bhavcopy import _build_session, _trading_days, BROWSER_HEADERS  # noqa: E402
from download_fo_bhavcopy import _candidate_urls, _norm_cols  # noqa: E402

_REPO = Path(__file__).resolve().parents[2]
_DEFAULT_BASE = _REPO / "backtest-data"

SOURCE_NAME = "nse_fo_bhavcopy"
TRUST_LEVEL = "HIGH"
FUT_SYMBOLS = ["NIFTY", "BANKNIFTY"]

FUT_SCHEMA = pa.schema([
    ("trade_date", pa.date32()), ("expiry", pa.date32()), ("underlying", pa.string()),
    ("open", pa.float64()), ("high", pa.float64()), ("low", pa.float64()),
    ("close", pa.float64()), ("settle", pa.float64()), ("oi", pa.int64()), ("volume", pa.int64()),
])


def _parse_legacy_fut(df: pd.DataFrame, td: date) -> pd.DataFrame:
    c = _norm_cols(df)
    need = ["INSTRUMENT", "SYMBOL", "EXPIRY_DT", "CLOSE"]
    if not all(k in c for k in need):
        return pd.DataFrame()
    m = df[(df[c["INSTRUMENT"]].astype(str).str.upper() == "FUTIDX") &
           (df[c["SYMBOL"]].astype(str).str.upper().isin(FUT_SYMBOLS))].copy()
    if m.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "trade_date": td, "expiry": pd.to_datetime(m[c["EXPIRY_DT"]], errors="coerce").dt.date,
        "underlying": m[c["SYMBOL"]].astype(str).str.upper(),
        "open": pd.to_numeric(m.get(c.get("OPEN"), pd.NA), errors="coerce"),
        "high": pd.to_numeric(m.get(c.get("HIGH"), pd.NA), errors="coerce"),
        "low": pd.to_numeric(m.get(c.get("LOW"), pd.NA), errors="coerce"),
        "close": pd.to_numeric(m[c["CLOSE"]], errors="coerce"),
        "settle": pd.to_numeric(m.get(c.get("SETTLE_PR"), m[c["CLOSE"]]), errors="coerce"),
        "oi": pd.to_numeric(m.get(c.get("OPEN_INT"), 0), errors="coerce"),
        "volume": pd.to_numeric(m.get(c.get("CONTRACTS"), 0), errors="coerce"),
    })


def _parse_udiff_fut(df: pd.DataFrame, td: date) -> pd.DataFrame:
    c = _norm_cols(df)
    need = ["FININSTRMTP", "TCKRSYMB", "XPRYDT", "CLSPRIC"]
    if not all(k in c for k in need):
        return pd.DataFrame()
    tp = df[c["FININSTRMTP"]].astype(str).str.upper()
    m = df[(tp == "IDF") & (df[c["TCKRSYMB"]].astype(str).str.upper().isin(FUT_SYMBOLS))].copy()
    if m.empty:
        return pd.DataFrame()
    return pd.DataFrame({
        "trade_date": td, "expiry": pd.to_datetime(m[c["XPRYDT"]], errors="coerce").dt.date,
        "underlying": m[c["TCKRSYMB"]].astype(str).str.upper(),
        "open": pd.to_numeric(m.get(c.get("OPNPRIC"), pd.NA), errors="coerce"),
        "high": pd.to_numeric(m.get(c.get("HGHPRIC"), pd.NA), errors="coerce"),
        "low": pd.to_numeric(m.get(c.get("LWPRIC"), pd.NA), errors="coerce"),
        "close": pd.to_numeric(m[c["CLSPRIC"]], errors="coerce"),
        "settle": pd.to_numeric(m.get(c.get("STTLMPRIC"), m[c["CLSPRIC"]]), errors="coerce"),
        "oi": pd.to_numeric(m.get(c.get("OPNINTRST"), 0), errors="coerce"),
        "volume": pd.to_numeric(m.get(c.get("TTLTRADGVOL"), 0), errors="coerce"),
    })


def _parse_zip_bytes(raw: bytes, td: date) -> pd.DataFrame:
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        df = pd.read_csv(io.BytesIO(z.read(z.namelist()[0])))
    out = _parse_udiff_fut(df, td)
    if out.empty:
        out = _parse_legacy_fut(df, td)
    out = out.dropna(subset=["expiry", "close"])
    for col in ("oi", "volume"):
        out[col] = out[col].fillna(0).astype("int64")
    return out.reset_index(drop=True)


def _date_dir(base: Path, underlying: str, d: date) -> Path:
    return base / "lake" / "futures" / f"underlying={underlying}" / f"date={d.isoformat()}"


def _write_day(base: Path, d: date, df: pd.DataFrame) -> int:
    n = 0
    for sym, grp in df.groupby("underlying"):
        out_dir = _date_dir(base, sym, d)
        out_dir.mkdir(parents=True, exist_ok=True)
        tbl = pa.Table.from_pandas(grp[[f.name for f in FUT_SCHEMA]], schema=FUT_SCHEMA, preserve_index=False)
        pq.write_table(tbl, out_dir / "part-0.parquet", compression="snappy")
        n += len(grp)
    return n


def _have_all(base: Path, d: date) -> bool:
    return all((_date_dir(base, s, d) / "part-0.parquet").exists() for s in FUT_SYMBOLS)


def _download_day(session, d: date, base: Path, force: bool) -> int:
    if _have_all(base, d) and not force:
        return -1
    all_404 = True
    last = ""
    for url in _candidate_urls(d):
        try:
            resp = session.get(url, timeout=30, headers=BROWSER_HEADERS)
            if resp.status_code == 200 and resp.content[:2] == b"PK":
                return _write_day(base, d, _parse_zip_bytes(resp.content, d))
            last = f"HTTP {resp.status_code}"
            if resp.status_code != 404:
                all_404 = False
        except Exception as exc:
            last = str(exc); all_404 = False
        time.sleep(0.3)
    if all_404:
        return -2
    print(f"    {d}: FAILED ({last})", file=sys.stderr)
    return 0


def _zip_csv(df: pd.DataFrame, name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, df.to_csv(index=False))
    return buf.getvalue()


def _self_test() -> int:
    print("SELF-TEST: F&O futures parsing (both formats)...")
    legacy = pd.DataFrame({
        "INSTRUMENT": ["FUTIDX", "FUTIDX", "OPTIDX", "FUTSTK"],
        "SYMBOL": ["NIFTY", "BANKNIFTY", "NIFTY", "RELIANCE"],
        "EXPIRY_DT": ["25-Jan-2023"] * 4, "OPEN": [18000, 42000, 0, 2500],
        "HIGH": [18100, 42200, 0, 2520], "LOW": [17900, 41800, 0, 2480],
        "CLOSE": [18050, 42100, 100, 2510], "SETTLE_PR": [18050, 42100, 100, 2510],
        "OPEN_INT": [1e7, 5e6, 1e6, 2e5], "CONTRACTS": [50000, 20000, 1000, 500],
    })
    out = _parse_zip_bytes(_zip_csv(legacy, "fo25JAN2023bhav.csv"), date(2023, 1, 25))
    assert set(out["underlying"]) == {"NIFTY", "BANKNIFTY"}, out["underlying"].tolist()
    assert len(out) == 2, len(out)
    print(f"  legacy: {len(out)} index-futures rows (OPTIDX + stock-fut filtered) ✅")

    udiff = pd.DataFrame({
        "TradDt": ["2024-09-26"] * 3, "FinInstrmTp": ["IDF", "IDF", "IDO"],
        "TckrSymb": ["NIFTY", "BANKNIFTY", "NIFTY"], "XpryDt": ["2024-09-26"] * 3,
        "OpnPric": [25000, 52000, 80], "HghPric": [25100, 52200, 90], "LwPric": [24900, 51800, 60],
        "ClsPric": [25050, 52100, 75], "SttlmPric": [25050, 52100, 75],
        "OpnIntrst": [1e7, 5e6, 6e4], "TtlTradgVol": [50000, 20000, 1200],
    })
    out2 = _parse_zip_bytes(_zip_csv(udiff, "BhavCopy_NSE_FO_0_0_0_20240926_F_0000.csv"), date(2024, 9, 26))
    assert set(out2["underlying"]) == {"NIFTY", "BANKNIFTY"} and len(out2) == 2
    print(f"  UDiFF: {len(out2)} index-futures rows ✅")

    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp); _write_day(base, date(2023, 1, 25), out)
        p = _date_dir(base, "NIFTY", date(2023, 1, 25)) / "part-0.parquet"
        assert p.exists() and len(pd.read_parquet(p)) == 1
    print("  lake write/read round-trip ✅\nSELF-TEST PASSED.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Download NSE index-futures bhavcopy → futures lake")
    ap.add_argument("--start", default="2022-06-01")
    ap.add_argument("--end", default=date.today().isoformat())
    ap.add_argument("--base", default=str(_DEFAULT_BASE))
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    args = ap.parse_args()
    if args.self_test:
        return _self_test()

    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    base = Path(args.base)
    days = _trading_days(start, end)
    print("=" * 72)
    print("QuantEmbrace — NSE index-FUTURES bhavcopy → futures lake (F1-full). Backtest-only.")
    print("=" * 72)
    print(f"  Period: {start} → {end} ({len(days)} trading days) · symbols {FUT_SYMBOLS}\n")

    session = _build_session()
    rows = days_ok = skip = holiday = fail = 0
    manifest = []
    for i, d in enumerate(days, 1):
        n = _download_day(session, d, base, args.force)
        if n == -1:
            skip += 1
        elif n == -2:
            holiday += 1
        elif n > 0:
            rows += n; days_ok += 1; manifest.append({"date": d.isoformat(), "rows": n})
        else:
            fail += 1
        if i % 50 == 0 or i == len(days):
            print(f"  …{i}/{len(days)} · {days_ok} downloaded ({rows:,} rows) · {skip} have · "
                  f"{holiday} holidays · {fail} failed")
        if i % 200 == 0:
            session = _build_session()

    mdir = base / "raw" / "fo_futures"
    mdir.mkdir(parents=True, exist_ok=True)
    (mdir / "_manifest.json").write_text(json.dumps({
        "source": SOURCE_NAME, "trust_level": TRUST_LEVEL, "symbols": FUT_SYMBOLS,
        "days": days_ok, "rows": rows, "fetched_at": datetime.now().isoformat(), "entries": manifest,
    }, indent=2))
    print(f"\nDone: {rows:,} index-futures rows across {days_ok} days ({holiday} holidays, {fail} failed).")
    if fail:
        print(f"  ⚠️ {fail} non-404 failures — re-run (idempotent) or paste a date to check the URL.")
    print(f"Lake: {base / 'lake' / 'futures'}")
    print("Next:  python scripts/backtest/run_overnight_futures_study.py --futures")
    print("Backtest-only. Advisory. No live trading.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
