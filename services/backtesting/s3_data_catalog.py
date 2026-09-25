"""S3 data catalog for the QuantEmbrace historical backtesting lab.

Defines the canonical lake layout, source **trust tiers**, and the **trusted vs
quarantine** zones described in ``docs/backtesting/aws-data-lake-contract.md``.

Design rules (backtest-only):
    * No broker APIs, no live/paper buckets, no DynamoDB/live state.
    * Path building only — this module performs no I/O.
    * Unknown sources default to LOW trust (safe default → quarantine).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum

# Supported candle intervals for the lab.
SUPPORTED_INTERVALS: tuple[str, ...] = ("1m", "5m", "15m", "1d")


class TrustLevel(str, Enum):
    """Provenance trust tier for a data source.

    HIGH — official NSE (e.g. Bhavcopy) or a licensed vendor. Usable for
           production strategy validation and model-training datasets.
    LOW  — GitHub/Kaggle/free/scraped/unknown. Must be quarantined and may not
           be used for strategy eligibility or model training until it passes
           quality + license review and is explicitly approved.
    """

    HIGH = "HIGH"
    LOW = "LOW"


class Zone(str, Enum):
    """Storage zone within the data lake."""

    LAKE = "lake"              # trusted, validated, engine-readable
    QUARANTINE = "quarantine"  # low-trust, NOT engine-readable until promoted
    RAW = "raw"               # immutable original drops
    REFERENCE = "reference"   # corp actions, symbol map, calendars, index membership


# Source-name → trust tier. Compared case-insensitively.
_HIGH_TRUST_SOURCES: frozenset[str] = frozenset(
    {
        "bhavcopy",
        "nse",
        "nse_bhavcopy",
        "nseindia",
        "nse_official",
        "truedata",
        "globaldatafeeds",
        "gdfl",
        "gfdl",
        "vendor",
        "licensed",
        # Zerodha Kite historical_data: exchange-validated candles from the
        # operator's own authorized broker feed. Classified HIGH per
        # aws-data-lake-contract.md §1 (the "gap-fill" tier). NOTE: trust is
        # about provenance quality, not coverage — Kite intraday depth is
        # limited (~3 yr, liquid names), so it is a limited-depth intraday
        # source for edge exploration, never a 15-yr authoritative backbone.
        "zerodha",
        "zerodha_kite",
        "kite",
    }
)

_LOW_TRUST_SOURCES: frozenset[str] = frozenset(
    {
        "github",
        "kaggle",
        "free",
        "scraped",
        "community",
        "unknown",
    }
)


def classify_source_trust(source: str | None) -> TrustLevel:
    """Classify a source name into a trust tier.

    Unknown / unrecognised / empty sources resolve to ``LOW`` (safe default).
    """
    if not source:
        return TrustLevel.LOW
    key = source.strip().lower()
    if key in _HIGH_TRUST_SOURCES:
        return TrustLevel.HIGH
    # Everything not explicitly trusted is LOW — including unrecognised names.
    return TrustLevel.LOW


def is_quarantined(trust: TrustLevel) -> bool:
    """Return True if data at this trust level must live in the quarantine zone."""
    return trust is not TrustLevel.HIGH


def is_s3_uri(uri: str) -> bool:
    """True if ``uri`` is an ``s3://`` URI."""
    return uri.startswith("s3://")


def split_s3_uri(uri: str) -> tuple[str, str]:
    """Split ``s3://bucket/key/prefix`` into ``(bucket, key_prefix)``."""
    if not is_s3_uri(uri):
        raise ValueError(f"Not an s3 URI: {uri!r}")
    rest = uri[len("s3://") :]
    bucket, _, key = rest.partition("/")
    return bucket, key


@dataclass(frozen=True)
class DataCatalog:
    """Builds canonical lake / quarantine / reference paths under a base URI.

    ``data_base`` may be a local directory or an ``s3://bucket[/prefix]``. All
    methods return a path/URI joined to the base; no method performs I/O.
    """

    data_base: str = "s3://quantembrace-backtest-data"

    # ── joining ──────────────────────────────────────────────────────────────
    def _join(self, *parts: str) -> str:
        base = self.data_base.rstrip("/")
        tail = "/".join(p.strip("/") for p in parts if p != "")
        return f"{base}/{tail}"

    # ── zones ────────────────────────────────────────────────────────────────
    def zone_for(self, trust: TrustLevel) -> Zone:
        """HIGH-trust data goes to the lake; everything else to quarantine."""
        return Zone.LAKE if trust is TrustLevel.HIGH else Zone.QUARANTINE

    # ── curated lake (Hive-partitioned, trusted only) ─────────────────────────
    def lake_partition(
        self,
        *,
        symbol: str,
        interval: str,
        year: int,
        segment: str = "EQ",
        market: str = "NSE",
    ) -> str:
        """Return the lake prefix for one ``(symbol, interval, year)`` partition."""
        self._validate_interval(interval)
        return self._join(
            "lake",
            "ohlcv",
            f"market={market}",
            f"segment={segment}",
            f"symbol={symbol}",
            f"interval={interval}",
            f"year={year}",
        )

    def lake_prefixes_for_range(
        self,
        *,
        symbol: str,
        interval: str,
        date_from: date,
        date_to: date,
        segment: str = "EQ",
        market: str = "NSE",
    ) -> list[str]:
        """Year-partition prefixes covering an inclusive date range."""
        self._validate_interval(interval)
        if date_to < date_from:
            raise ValueError("date_to must be >= date_from")
        return [
            self.lake_partition(
                symbol=symbol, interval=interval, year=y, segment=segment, market=market
            )
            for y in range(date_from.year, date_to.year + 1)
        ]

    # ── quarantine (low-trust landing zone) ───────────────────────────────────
    def quarantine_prefix(self, *, source: str, ingest_date: date | str) -> str:
        d = ingest_date.isoformat() if isinstance(ingest_date, date) else str(ingest_date)
        return self._join("quarantine", source, d)

    def raw_prefix(self, *, source: str, ingest_date: date | str) -> str:
        d = ingest_date.isoformat() if isinstance(ingest_date, date) else str(ingest_date)
        return self._join("raw", source, d)

    # ── reference data ────────────────────────────────────────────────────────
    def corporate_actions(self, symbol: str) -> str:
        return self._join("reference", "corporate_actions", f"{symbol}.parquet")

    def symbol_map(self) -> str:
        return self._join("reference", "symbol_map", "isin_map.parquet")

    def index_membership(self, index: str, effective_date: date | str) -> str:
        d = effective_date.isoformat() if isinstance(effective_date, date) else str(effective_date)
        return self._join("reference", "index_membership", index, f"{d}.parquet")

    def trading_calendar(self) -> str:
        return self._join("reference", "calendars", "nse_trading_calendar.parquet")

    def snapshot_manifest(self, snapshot_id: str) -> str:
        return self._join("_snapshots", f"{snapshot_id}.json")

    # ── helpers ──────────────────────────────────────────────────────────────
    @staticmethod
    def _validate_interval(interval: str) -> None:
        if interval not in SUPPORTED_INTERVALS:
            raise ValueError(
                f"Unsupported interval {interval!r}; supported: {SUPPORTED_INTERVALS}"
            )
