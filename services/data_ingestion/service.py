"""
Data Ingestion Service — main service orchestrator.

Manages the lifecycle of market data connectors, tick processor, storage
writers, and the SQS tick publisher. Designed for graceful startup/shutdown
on AWS ECS Fargate with restart-safety (no data loss on container restarts).

Signal flow out of this service:
    Zerodha WebSocket  ──┐
                         ├──▶ TickProcessor ──▶ S3Writer  (historical)
    Alpaca WebSocket   ──┘          │        ──▶ DynamoWriter (latest price)
                                    └────────▶ SQSTickPublisher ──▶ SQS
                                                                      │
                                                               StrategyEngine

Instrument universe:
    Which symbols are subscribed is driven entirely by
    ``configs/instruments.yaml``. Add or remove symbols there and restart
    the service — no code changes required.
"""

from __future__ import annotations

import asyncio
import signal
from pathlib import Path
from typing import Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger, set_correlation_id

from data_ingestion.connectors.alpaca_connector import AlpacaConnector
from data_ingestion.connectors.base import BaseConnector
from data_ingestion.connectors.zerodha_connector import ZerodhaConnector
from data_ingestion.processors.tick_processor import TickProcessor
from data_ingestion.publishers.sqs_publisher import SQSTickPublisher
from data_ingestion.storage.dynamo_writer import DynamoWriter
from data_ingestion.storage.s3_writer import S3Writer

logger = get_logger(__name__, service_name="data_ingestion")

# Path to the instrument universe config — overridable via env for tests
import os as _os
_DEFAULT_INSTRUMENTS_CONFIG = (
    Path(__file__).parent.parent.parent / "configs" / "instruments.yaml"
)


class DataIngestionService:
    """
    Orchestrates market data ingestion from Zerodha (NSE) and Alpaca (US).

    Lifecycle:
        1. ``start()``
            a. Initialize storage writers (S3, DynamoDB).
            b. Initialize SQS tick publisher.
            c. Initialize tick processor (wired to all backends).
            d. Initialize broker connectors.
            e. Connect all connectors (parallel).
            f. Load active symbol list from instruments.yaml.
            g. Subscribe connectors to their respective symbol lists.
            h. Wait for shutdown signal.

        2. ``stop()``
            a. Disconnect all connectors.
            b. Stop SQS publisher (final flush).
            c. Flush S3 and DynamoDB write buffers.
            d. Set shutdown event.

    Restart-safety:
        - S3 writes are keyed by timestamp — re-uploading overwrites with
          identical data, so partial batches are safe to retry.
        - DynamoDB writes use latest-value semantics — re-sending the same
          tick just overwrites with the same value.
        - SQS deduplication via MessageDeduplicationId prevents duplicate
          strategy engine processing.
    """

    def __init__(
        self,
        settings: Optional[AppSettings] = None,
        instruments_config_path: Optional[str] = None,
    ) -> None:
        """
        Initialize the data ingestion service.

        Args:
            settings: Application settings. Loaded from environment if None.
            instruments_config_path: Override path for instruments.yaml.
        """
        self._settings = settings or get_settings()
        self._instruments_config_path = (
            instruments_config_path
            or _os.environ.get("INSTRUMENTS_CONFIG_PATH")
            or str(_DEFAULT_INSTRUMENTS_CONFIG)
        )
        self._connectors: list[BaseConnector] = []
        self._tick_processor: Optional[TickProcessor] = None
        self._sqs_publisher: Optional[SQSTickPublisher] = None
        self._s3_writer: Optional[S3Writer] = None
        self._dynamo_writer: Optional[DynamoWriter] = None
        self._running = False
        self._shutdown_event = asyncio.Event()

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the data ingestion service end-to-end.

        Connects all broker WebSockets and begins streaming live market data
        through the processing pipeline to storage and the strategy engine.
        """
        set_correlation_id()
        logger.info("Starting Data Ingestion Service")

        # ── 1. Storage backends ───────────────────────────────────────────────
        self._s3_writer = S3Writer(
            bucket=self._settings.aws.s3_bucket,
            region=self._settings.aws.region,
        )
        self._dynamo_writer = DynamoWriter(
            table_name=self._settings.aws.dynamodb_table_prices,
            region=self._settings.aws.region,
        )

        # ── 2. SQS tick publisher ─────────────────────────────────────────────
        self._sqs_publisher = SQSTickPublisher(
            queue_url=self._settings.aws.sqs_market_data_queue,
            flush_interval=1.0,   # publish latest price per symbol every second
        )
        await self._sqs_publisher.start()

        # ── 3. Tick processor (wired to all backends) ─────────────────────────
        self._tick_processor = TickProcessor(
            s3_writer=self._s3_writer,
            dynamo_writer=self._dynamo_writer,
            sqs_publisher=self._sqs_publisher,
        )

        # ── 4. Broker connectors ──────────────────────────────────────────────
        zerodha = ZerodhaConnector(
            api_key=self._settings.zerodha.api_key.get_secret_value(),
            access_token=self._settings.zerodha.access_token.get_secret_value(),
            on_tick=self._tick_processor.process_tick,
        )
        alpaca = AlpacaConnector(
            api_key=self._settings.alpaca.api_key.get_secret_value(),
            api_secret=self._settings.alpaca.api_secret.get_secret_value(),
            base_url=self._settings.alpaca.data_url,
            on_tick=self._tick_processor.process_tick,
        )
        self._connectors = [zerodha, alpaca]

        # ── 5. Register OS signal handlers for ECS SIGTERM ───────────────────
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # ── 6. Connect all connectors (in parallel) ───────────────────────────
        logger.info("Connecting broker WebSockets")
        connect_results = await asyncio.gather(
            *[connector.connect() for connector in self._connectors],
            return_exceptions=True,
        )
        for connector, result in zip(self._connectors, connect_results):
            if isinstance(result, Exception):
                logger.error(
                    "Connector %s failed to connect: %s",
                    connector.__class__.__name__,
                    result,
                )
            else:
                logger.info(
                    "Connector %s connected", connector.__class__.__name__
                )

        # ── 7. Load instrument universe and subscribe ─────────────────────────
        await self._subscribe_to_instruments()

        self._running = True
        logger.info(
            "Data Ingestion Service started — "
            "streaming live data for %d connector(s)",
            sum(1 for c in self._connectors if c.is_connected),
        )

        # Hold until SIGTERM/SIGINT
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """
        Gracefully stop the data ingestion service.

        Disconnects all connectors, stops the SQS publisher (with a final
        flush), and drains S3/DynamoDB write buffers.
        """
        if not self._running:
            return

        logger.info("Stopping Data Ingestion Service")
        self._running = False

        # Disconnect connectors
        for connector in self._connectors:
            try:
                await connector.disconnect()
            except Exception:
                logger.exception(
                    "Error disconnecting %s", connector.__class__.__name__
                )

        # Stop SQS publisher (flushes remaining ticks)
        if self._sqs_publisher is not None:
            await self._sqs_publisher.stop()

        # Flush S3 and DynamoDB write buffers
        if self._s3_writer is not None:
            await self._s3_writer.flush()
        if self._dynamo_writer is not None:
            await self._dynamo_writer.flush()

        self._shutdown_event.set()
        logger.info("Data Ingestion Service stopped")

    # ── Instrument subscription ───────────────────────────────────────────────

    async def _subscribe_to_instruments(self) -> None:
        """
        Load the active instrument universe from instruments.yaml and
        subscribe each connector to its market's symbol list.

        Zerodha  → NSE symbols
        Alpaca   → US symbols

        Logs a warning (but does not crash) if the config file is missing
        or a connector subscription fails — the service can run with a
        partial subscription.
        """
        try:
            from strategy_engine.universe.instrument_loader import InstrumentLoader
        except ImportError:
            logger.warning(
                "InstrumentLoader not importable — falling back to env-based symbol lists"
            )
            await self._subscribe_from_env()
            return

        try:
            loader = InstrumentLoader(config_path=self._instruments_config_path)
            loader.load()
            logger.info("Loaded instrument universe:\n%s", loader.summary())
        except Exception:
            logger.exception(
                "Failed to load instruments.yaml — no subscriptions made. "
                "Check INSTRUMENTS_CONFIG_PATH or configs/instruments.yaml."
            )
            return

        nse_symbols = loader.get_all_symbols("NSE")
        us_symbols  = loader.get_all_symbols("US")

        if nse_symbols and self._connectors:
            zerodha_connector = next(
                (c for c in self._connectors if isinstance(c, ZerodhaConnector)),
                None,
            )
            if zerodha_connector and zerodha_connector.is_connected:
                try:
                    await zerodha_connector.subscribe(nse_symbols)
                except Exception:
                    logger.exception(
                        "Zerodha subscription failed for symbols: %s", nse_symbols
                    )
            else:
                logger.warning(
                    "Zerodha connector is not connected — NSE subscription skipped"
                )

        if us_symbols and self._connectors:
            alpaca_connector = next(
                (c for c in self._connectors if isinstance(c, AlpacaConnector)),
                None,
            )
            if alpaca_connector and alpaca_connector.is_connected:
                try:
                    await alpaca_connector.subscribe(us_symbols)
                except Exception:
                    logger.exception(
                        "Alpaca subscription failed for symbols: %s", us_symbols
                    )
            else:
                logger.warning(
                    "Alpaca connector is not connected — US subscription skipped"
                )

    async def _subscribe_from_env(self) -> None:
        """
        Fallback subscription using comma-separated env vars.

        Environment variables:
            SUBSCRIBE_NSE_SYMBOLS  — e.g., "RELIANCE,TCS,INFY"
            SUBSCRIBE_US_SYMBOLS   — e.g., "AAPL,MSFT,GOOGL"

        Used when InstrumentLoader is not importable (e.g., running data
        ingestion as a standalone service without the full monorepo layout).
        """
        nse_raw = _os.environ.get("SUBSCRIBE_NSE_SYMBOLS", "")
        us_raw  = _os.environ.get("SUBSCRIBE_US_SYMBOLS", "")

        nse_symbols = [s.strip() for s in nse_raw.split(",") if s.strip()]
        us_symbols  = [s.strip() for s in us_raw.split(",") if s.strip()]

        for connector in self._connectors:
            if isinstance(connector, ZerodhaConnector) and nse_symbols:
                try:
                    await connector.subscribe(nse_symbols)
                except Exception:
                    logger.exception("Zerodha fallback subscription failed")
            elif isinstance(connector, AlpacaConnector) and us_symbols:
                try:
                    await connector.subscribe(us_symbols)
                except Exception:
                    logger.exception("Alpaca fallback subscription failed")

    # ── Properties ────────────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        """Whether the service is currently running."""
        return self._running


async def main() -> None:
    """Entry point for the Data Ingestion Service."""
    service = DataIngestionService()
    await service.start()


if __name__ == "__main__":
    asyncio.run(main())
