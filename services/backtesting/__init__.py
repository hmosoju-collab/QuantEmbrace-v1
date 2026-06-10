"""QuantEmbrace historical backtesting lab — data layer.

Backtest-only package. Modules here ingest, locate, load, and validate historical
NSE OHLCV data for offline backtesting. Nothing in this package calls broker APIs,
touches live/paper tables, or enables live trading.

Public surface:
    s3_data_catalog : canonical lake/quarantine layout + source trust tiers
    data_loader     : load CSV/Parquet from local or S3 into the canonical schema
    data_quality    : the NSE OHLCV quality checks + markdown report renderer

See docs/backtesting/aws-data-lake-contract.md for the governing contract.
"""

from __future__ import annotations

__all__ = [
    "s3_data_catalog",
    "data_loader",
    "data_quality",
]
