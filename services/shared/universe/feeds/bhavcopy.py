"""
NSE Bhavcopy downloader — daily OHLCV + delivery data.

Bhavcopy (Bhav = price, copy = record) is NSE's daily equity price file published
after market close. It is available as a ZIP of CSV from the NSE public archives
and does NOT require session cookies.

  Bhavcopy URL:
    https://archives.nseindia.com/content/historical/EQUITIES/{YYYY}/{MMM}/cm{DD}{MMM}{YYYY}bhav.csv.zip
    e.g. https://archives.nseindia.com/content/historical/EQUITIES/2026/MAY/cm26MAY2026bhav.csv.zip

  MTO (Market Turnover) — delivery position data:
    https://archives.nseindia.com/archives/equities/mto/MTO_{DDMMYYYY}.DAT

Bhavcopy CSV columns (EQ/BE series rows only):
  SYMBOL, SERIES, OPEN, HIGH, LOW, CLOSE, LAST, PREVCLOSE,
  TOTTRDQTY (shares), TOTTRDVAL (rupees), TIMESTAMP, TOTALTRADES, ISIN

MTO DAT format (record type 20):
  RecType, SrNo, SecurityName, DeliverableQty, TradedQty, %Deliverable, Spread

ADV is computed by averaging TOTTRDVAL (₹ crores) over the requested window of
trading days. The BhavcopySeries caches each day's parsed data on disk to avoid
re-downloading on subsequent builds.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Optional

import requests

from shared.universe.models import LiquidityMetrics

logger = logging.getLogger(__name__)

_BHAV_URL = (
    "https://archives.nseindia.com/content/historical/EQUITIES"
    "/{year}/{month}/cm{day}{month}{year}bhav.csv.zip"
)
_MTO_URL = (
    "https://archives.nseindia.com/archives/equities/mto/MTO_{ddmmyyyy}.DAT"
)
_ARCHIVE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.nseindia.com/",
}
_REQUEST_TIMEOUT = 30


@dataclass
class _BhavRow:
    symbol: str
    series: str
    close: float
    tottrdval: float   # rupees
    tottrdqty: int     # shares


@dataclass
class _DayCache:
    trading_date: date
    rows: dict[str, _BhavRow]        # symbol → row
    delivery_pct: dict[str, float]   # symbol → fraction (0.0–1.0)


# ── Low-level fetch helpers ───────────────────────────────────────────────────

def _fetch_bhavcopy(trading_date: date) -> Optional[bytes]:
    """Download Bhavcopy ZIP for a given date. Returns None on 404 (holiday/future)."""
    url = _BHAV_URL.format(
        year=trading_date.strftime("%Y"),
        month=trading_date.strftime("%b").upper(),
        day=trading_date.strftime("%d"),
    )
    try:
        resp = requests.get(url, headers=_ARCHIVE_HEADERS, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.content
    except requests.HTTPError:
        return None
    except Exception as exc:
        logger.warning("bhavcopy.fetch_failed date=%s error=%s", trading_date, exc)
        return None


def _parse_bhavcopy(data: bytes) -> dict[str, _BhavRow]:
    """Parse a Bhavcopy ZIP and return EQ/BE rows keyed by symbol."""
    rows: dict[str, _BhavRow] = {}
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for name in zf.namelist():
            if not name.endswith(".csv"):
                continue
            with zf.open(name) as f:
                text = f.read().decode("utf-8", errors="replace")
            reader = csv.DictReader(io.StringIO(text))
            for row in reader:
                series = row.get("SERIES", "").strip()
                if series not in ("EQ", "BE"):
                    continue
                sym = row.get("SYMBOL", "").strip().upper()
                if not sym:
                    continue
                try:
                    close = float(row.get("CLOSE") or 0)
                    tottrdval = float(row.get("TOTTRDVAL") or 0)
                    tottrdqty = int(float(row.get("TOTTRDQTY") or 0))
                except (ValueError, TypeError):
                    continue
                rows[sym] = _BhavRow(
                    symbol=sym, series=series, close=close,
                    tottrdval=tottrdval, tottrdqty=tottrdqty,
                )
    return rows


def _fetch_mto(trading_date: date) -> dict[str, float]:
    """
    Download NSE MTO (delivery position) file for a trading date.
    Returns symbol → delivery fraction (0.0–1.0). Empty dict on failure.
    """
    url = _MTO_URL.format(ddmmyyyy=trading_date.strftime("%d%m%Y"))
    delivery: dict[str, float] = {}
    try:
        resp = requests.get(url, headers=_ARCHIVE_HEADERS, timeout=_REQUEST_TIMEOUT)
        if resp.status_code == 404:
            return delivery
        resp.raise_for_status()
        for line in resp.text.splitlines():
            parts = [p.strip() for p in line.split(",")]
            # MTO format (record type 20):
            # RecType,SrNo,SecurityName,DeliverableQty,TradedQty,%Deliverable,Spread
            if len(parts) < 6 or parts[0] != "20":
                continue
            sym = parts[2].strip().upper()
            try:
                pct_str = parts[5].strip().rstrip("%")
                delivery[sym] = float(pct_str) / 100.0
            except (ValueError, IndexError):
                continue
    except Exception as exc:
        logger.warning("bhavcopy.mto_failed date=%s error=%s", trading_date, exc)
    return delivery


# ── Disk cache helpers ────────────────────────────────────────────────────────

def _cache_path(cache_dir: Path, d: date) -> Path:
    return cache_dir / f"bhav_{d.isoformat()}.json"


def _load_from_disk(cache_dir: Path, d: date) -> Optional[_DayCache]:
    p = _cache_path(cache_dir, d)
    if not p.exists():
        return None
    try:
        raw = json.loads(p.read_text())
        rows = {
            sym: _BhavRow(
                symbol=sym, series=r["s"], close=r["c"],
                tottrdval=r["v"], tottrdqty=r["q"],
            )
            for sym, r in raw["rows"].items()
        }
        return _DayCache(
            trading_date=d, rows=rows,
            delivery_pct={k: float(v) for k, v in raw.get("d", {}).items()},
        )
    except Exception as exc:
        logger.warning("bhavcopy.disk_load_failed date=%s error=%s", d, exc)
        return None


def _save_to_disk(cache_dir: Path, entry: _DayCache) -> None:
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        raw = {
            "rows": {
                sym: {"s": r.series, "c": r.close, "v": r.tottrdval, "q": r.tottrdqty}
                for sym, r in entry.rows.items()
            },
            "d": entry.delivery_pct,
        }
        _cache_path(cache_dir, entry.trading_date).write_text(json.dumps(raw))
    except Exception as exc:
        logger.warning("bhavcopy.disk_save_failed date=%s error=%s", entry.trading_date, exc)


# ── BhavcopySeries ────────────────────────────────────────────────────────────

class BhavcopySeries:
    """
    Downloads and caches Bhavcopy + MTO data for a rolling window of trading days.

    Used by NseApiDataSource to compute LiquidityMetrics from real market data.

    Args:
        cache_dir: Optional directory for disk caching parsed Bhavcopy data.
                   Each day is a ~200KB JSON file. Strongly recommended for
                   production to avoid re-downloading 20 days on every restart.
    """

    def __init__(self, cache_dir: Optional[Path] = None) -> None:
        self._cache_dir = cache_dir
        self._mem: dict[date, _DayCache] = {}

    def _weekday_dates_before(self, as_of: date, needed: int) -> list[date]:
        """Return Mon–Fri dates going back from as_of (exclusive), yielding 3× needed."""
        dates: list[date] = []
        d = as_of - timedelta(days=1)
        limit = needed * 3
        while len(dates) < limit:
            if d.weekday() < 5:
                dates.append(d)
            d -= timedelta(days=1)
        return dates

    def _load_day(self, d: date) -> Optional[_DayCache]:
        """Load one day: memory → disk → NSE archive. Returns None for holidays."""
        if d in self._mem:
            return self._mem[d]
        if self._cache_dir is not None:
            entry = _load_from_disk(self._cache_dir, d)
            if entry is not None:
                self._mem[d] = entry
                return entry

        raw = _fetch_bhavcopy(d)
        if raw is None:
            return None  # Holiday or future date

        rows = _parse_bhavcopy(raw)
        delivery = _fetch_mto(d)
        entry = _DayCache(trading_date=d, rows=rows, delivery_pct=delivery)
        self._mem[d] = entry

        if self._cache_dir is not None:
            _save_to_disk(self._cache_dir, entry)

        logger.info(
            "bhavcopy.loaded date=%s symbols=%d delivery_symbols=%d",
            d, len(rows), len(delivery),
        )
        return entry

    def fetch_window(self, as_of: date, window: int) -> list[_DayCache]:
        """
        Return up to `window` trading-day entries before `as_of`.

        Skips dates where NSE returned 404 (exchange holidays).
        """
        candidates = self._weekday_dates_before(as_of, window)
        entries: list[_DayCache] = []
        for d in candidates:
            if len(entries) >= window:
                break
            entry = self._load_day(d)
            if entry is not None:
                entries.append(entry)
        return entries

    def compute_liquidity_metrics(
        self,
        symbol: str,
        as_of: date,
        window_20: int = 20,
        window_60: int = 60,
    ) -> Optional[LiquidityMetrics]:
        """
        Compute LiquidityMetrics for a symbol from historical Bhavcopy data.

        Returns None only if the symbol has never appeared in any fetched file.
        Returns LiquidityMetrics(data_available=False) if data was fetched but
        the symbol had no trades in the window.
        """
        sym = symbol.upper()
        entries_20 = self.fetch_window(as_of, window_20)

        if not entries_20:
            logger.warning(
                "bhavcopy.no_data_fetched as_of=%s — NSE archive unreachable?", as_of
            )
            return None

        # ── 20-day metrics ─────────────────────────────────────────────────────
        vals_20: list[float] = []
        vols_20: list[int] = []
        deliveries: list[float] = []
        zero_vol_days = 0
        last_close: Optional[float] = None
        active_20 = 0

        for entry in entries_20:
            row = entry.rows.get(sym)
            if row is None:
                continue
            active_20 += 1
            crores = row.tottrdval / 1e7
            vals_20.append(crores)
            vols_20.append(row.tottrdqty)
            if row.tottrdqty == 0:
                zero_vol_days += 1
            if last_close is None:
                last_close = row.close
            d_pct = entry.delivery_pct.get(sym)
            if d_pct is not None:
                deliveries.append(d_pct)

        if not vals_20:
            return LiquidityMetrics(symbol=sym, trading_date=as_of, data_available=False)

        adv_20 = round(sum(vals_20) / len(vals_20), 2)
        adv_vol_20 = int(sum(vols_20) / len(vols_20))
        avg_delivery = round(sum(deliveries) / len(deliveries), 4) if deliveries else None

        # ── 60-day metrics (best-effort) ────────────────────────────────────────
        entries_60 = self.fetch_window(as_of, window_60)
        adv_60: Optional[float] = None
        adv_vol_60: Optional[int] = None
        active_60: Optional[int] = None

        if len(entries_60) >= 30:
            vals_60, vols_60 = [], []
            count_60 = 0
            for entry in entries_60:
                row = entry.rows.get(sym)
                if row:
                    count_60 += 1
                    vals_60.append(row.tottrdval / 1e7)
                    vols_60.append(row.tottrdqty)
            if vals_60:
                adv_60 = round(sum(vals_60) / len(vals_60), 2)
                adv_vol_60 = int(sum(vols_60) / len(vols_60))
                active_60 = count_60

        return LiquidityMetrics(
            symbol=sym,
            trading_date=as_of,
            adv_crores_20d=adv_20,
            adv_volume_20d=adv_vol_20,
            adv_crores_60d=adv_60,
            adv_volume_60d=adv_vol_60,
            active_days_last_20=active_20,
            active_days_last_60=active_60,
            delivery_pct_20d=avg_delivery,
            zero_volume_days_last_20=zero_vol_days,
            data_available=True,
        )
