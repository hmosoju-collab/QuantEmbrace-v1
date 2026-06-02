"""
Data Ingestion Service — main service orchestrator.

Manages the lifecycle of market data connectors, tick processor, storage
writers, and Kafka tick publisher. Designed for graceful startup/shutdown
on AWS EC2 ASGs with restart-safety (no data loss on instance restarts).

Signal flow out of this service:

    Zerodha WebSocket  ──┐
                         ├──▶ TickProcessor ──▶ S3Writer       (historical archive)
    Alpaca WebSocket   ──┘          │        ──▶ DynamoWriter   (latest-price snapshot)
                                    └────────▶ KafkaTickPublisher ──▶ MSK (ticks.nse / ticks.us)

Instrument universe:
    Which symbols are subscribed is driven entirely by
    ``configs/instruments.yaml``. Add or remove symbols there and restart
    the service — no code changes required.

Required environment variables:
    KAFKA_BOOTSTRAP_SERVERS — MSK Serverless bootstrap endpoint (mandatory).
                              Service will refuse to start if not set.
"""

from __future__ import annotations

import asyncio
import os as _os
import signal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from data_ingestion.connectors.alpaca_connector import AlpacaConnector
from data_ingestion.connectors.base import BaseConnector
from data_ingestion.connectors.zerodha_connector import ZerodhaConnector
from data_ingestion.processors.tick_processor import TickProcessor
from data_ingestion.publishers.kafka_tick_publisher import KafkaTickPublisher
from data_ingestion.storage.dynamo_writer import DynamoWriter
from data_ingestion.storage.s3_writer import S3Writer
from shared.config.settings import AppSettings, get_settings
from shared.health.health_server import HealthServer
from shared.logging.logger import get_logger, set_correlation_id

if TYPE_CHECKING:
    from data_ingestion.candle_stream import IntradayCandleStream
    from data_ingestion.features.feature_archiver import FeatureArchiver
    from shared.zerodha.market_phase import MarketPhaseGovernor

logger = get_logger(__name__, service_name="data_ingestion")

# Path to the instrument universe config — overridable via env for tests
_DEFAULT_INSTRUMENTS_CONFIG = Path(__file__).parent.parent.parent / "configs" / "instruments.yaml"


class DataIngestionService:
    """
    Orchestrates market data ingestion from Zerodha (NSE) and Alpaca (US).

    Lifecycle:
        1. ``start()``
            a. Initialize storage writers (S3, DynamoDB).
            b. Initialize Kafka tick publisher (mandatory — fails fast if KAFKA_BOOTSTRAP_SERVERS unset).
            c. Initialize tick processor (wired to S3, DynamoDB, and Kafka).
            d. Initialize broker connectors.
            e. Connect all connectors (parallel).
            f. Load active symbol list from instruments.yaml.
            g. Subscribe connectors to their respective symbol lists.
            h. Register health checks and mark service ready.
            i. Wait for shutdown signal.

        2. ``stop()``
            a. Mark service not-ready (drains load balancer).
            b. Disconnect all connectors.
            c. Stop Kafka publisher (drain in-flight deliveries).
            d. Flush S3 and DynamoDB write buffers.
            e. Set shutdown event.

    Restart-safety:
        - S3 writes are keyed by timestamp — re-uploading overwrites with
          identical data, so partial batches are safe to retry.
        - DynamoDB writes use latest-value semantics — re-sending the same
          tick overwrites with the same value (idempotent).
        - Kafka producer uses enable.idempotence=true + acks=all; after a
          restart the same tick produces the same message key, allowing
          consumers to handle duplicates via sequence_id comparison.
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
        self._kafka_publisher: Optional[KafkaTickPublisher] = None
        self._s3_writer: Optional[S3Writer] = None
        self._dynamo_writer: Optional[DynamoWriter] = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        # Phase 5: feature pipeline
        self._candle_stream: Optional[IntradayCandleStream] = None
        self._feature_archiver: Optional[FeatureArchiver] = None
        self._phase_governor: Optional[MarketPhaseGovernor] = None
        self._candle_stream_task: Optional[asyncio.Task[None]] = None
        self._candle_stream_watchdog_task: Optional[asyncio.Task[None]] = None
        self._phase_governor_task: Optional[asyncio.Task[None]] = None
        self._governor_watchdog_task: Optional[asyncio.Task[None]] = None
        # ADR-021 Phase 1: producer heartbeat task — writes a DynamoDB key every
        # 10s so risk_engine's producer_heartbeat_monitor can detect a dead WebSocket
        # independently of Kafka consumer lag.
        self._producer_heartbeat_task: Optional[asyncio.Task[None]] = None
        # Health/readiness server — ASG target group polls :8080/health (liveness)
        # and :8080/ready (readiness) before marking instance InService.
        self._health_server = HealthServer(
            port=getattr(self._settings, "health_check_port", 8080),
            service_name="data_ingestion",
        )

    # ── Public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Start the data ingestion service end-to-end.

        Connects all broker WebSockets and begins streaming live market data
        through the processing pipeline to storage and Kafka.

        Raises:
            RuntimeError: If KAFKA_BOOTSTRAP_SERVERS is not set — the service
                          requires Kafka and will not start without it.
        """
        set_correlation_id()
        logger.info("Starting Data Ingestion Service")

        # ── 0. Validate mandatory configuration ──────────────────────────────
        kafka_bootstrap = _os.environ.get("KAFKA_BOOTSTRAP_SERVERS", "").strip()
        if not kafka_bootstrap:
            raise RuntimeError(
                "KAFKA_BOOTSTRAP_SERVERS is not set. "
                "This service requires MSK Serverless Kafka to operate. "
                "Set KAFKA_BOOTSTRAP_SERVERS to the MSK bootstrap endpoint and restart."
            )

        # ── 1. Start health server immediately (liveness from first second) ──
        # /health returns 200 as soon as the server starts.
        # /ready returns 503 until set_ready(True) is called after full startup.
        await self._health_server.start()

        # ── 2. Storage backends ───────────────────────────────────────────────
        self._s3_writer = S3Writer(
            bucket=self._settings.aws.s3_bucket,
            region=self._settings.aws.region,
        )
        self._dynamo_writer = DynamoWriter(
            table_name=self._settings.aws.dynamodb_table_prices,
            region=self._settings.aws.region,
        )

        # ── 3. Kafka tick publisher ───────────────────────────────────────────
        # Durable outbox: only enable in production.
        # In local dev/paper-trading the outbox writes ~60 DynamoDB put_item
        # calls/second to LocalStack, which cannot sustain that load — connections
        # time out after 10 seconds, filling the thread pool and preventing the
        # candle stream from getting thread pool slots for historical data fetches.
        # Production uses AWS DynamoDB + MSK Serverless which handle this load.
        _is_production = self._settings.environment == "production"
        self._kafka_publisher = KafkaTickPublisher(
            bootstrap_servers=kafka_bootstrap,
            aws_region=self._settings.aws.region,
            dynamodb_table_sessions=self._settings.aws.dynamodb_table_risk_state,
            environment=self._settings.environment,
            durable_outbox_enabled=_is_production,
        )
        await self._kafka_publisher.start()
        logger.info("Kafka tick publisher started (brokers=%s)", kafka_bootstrap)

        # ── 4. Tick processor (wired to all backends) ─────────────────────────
        _stale_threshold = float(
            _os.environ.get("DATA_INGESTION_TICK_STALE_THRESHOLD", "60.0")
        )
        self._tick_processor = TickProcessor(
            s3_writer=self._s3_writer,
            dynamo_writer=self._dynamo_writer,
            kafka_publisher=self._kafka_publisher,
            stale_threshold_seconds=_stale_threshold,
        )

        # ── 4.5 Resolve fresh Zerodha token from DynamoDB ────────────────────
        # ZerodhaTokenManager stores the daily token in DynamoDB after login.
        # Reading it here ensures ZerodhaConnector (WebSocket) uses a fresh
        # token instead of the potentially stale ZERODHA_ACCESS_TOKEN env var.
        _zerodha_access_token = self._settings.zerodha.access_token.get_secret_value()
        try:
            from execution_engine.auth.zerodha_auth import ZerodhaTokenManager  # noqa: PLC0415
            from shared.aws.clients import get_dynamodb_client  # noqa: PLC0415
            _token_mgr = ZerodhaTokenManager(
                dynamo_client=get_dynamodb_client(),
                settings=self._settings,
            )
            _zerodha_access_token = await _token_mgr.get_valid_token()
            logger.info("zerodha_connector.token_loaded_from_dynamodb")
        except Exception:
            logger.warning(
                "zerodha_connector.token_fallback_env_var",
                detail=(
                    "DynamoDB token lookup failed — ZerodhaConnector will use "
                    "ZERODHA_ACCESS_TOKEN env var. Run zerodha_login.py to refresh."
                ),
            )

        # ── 5. Broker connectors ──────────────────────────────────────────────
        zerodha = ZerodhaConnector(
            api_key=self._settings.zerodha.api_key.get_secret_value(),
            access_token=_zerodha_access_token,
            on_tick=self._tick_processor.process_tick,
        )
        alpaca = AlpacaConnector(
            api_key=self._settings.alpaca.api_key.get_secret_value(),
            api_secret=self._settings.alpaca.api_secret.get_secret_value(),
            base_url=self._settings.alpaca.data_url,
            on_tick=self._tick_processor.process_tick,
        )
        self._connectors = [zerodha, alpaca]

        # ── 6. Register OS signal handlers for EC2 instance termination ───────
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # ── 7. Connect all connectors (in parallel) ───────────────────────────
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
                logger.info("Connector %s connected", connector.__class__.__name__)

        # ── 8. Load instrument universe and subscribe ─────────────────────────
        await self._subscribe_to_instruments()

        # ── 9. Phase 5 feature pipeline ───────────────────────────────────────
        # Non-fatal: any failure inside is caught and logged; trading continues.
        await self._setup_feature_pipeline()

        # ── 9.5 ADR-021 Phase 1: producer heartbeat writer ───────────────────
        # Writes HEARTBEAT#{market}/CURRENT to latest-prices DynamoDB table every 10s
        # so risk_engine's producer_heartbeat_monitor can detect a dead WebSocket
        # feed independently of Kafka consumer lag.
        self._producer_heartbeat_task = asyncio.create_task(
            self._producer_heartbeat_loop(),
            name="data-ingestion-producer-heartbeat",
        )

        # ── 10. Register health checks and mark service ready ─────────────────
        self._health_server.add_check(
            "zerodha_connected",
            lambda: any(
                isinstance(c, ZerodhaConnector) and c.is_connected for c in self._connectors
            ),
        )
        self._health_server.add_check(
            "alpaca_connected",
            lambda: any(
                isinstance(c, AlpacaConnector) and c.is_connected for c in self._connectors
            ),
        )
        self._health_server.add_check(
            "tick_processor_ready",
            lambda: self._tick_processor is not None,
        )
        self._health_server.add_check(
            "kafka_publisher_ready",
            lambda: (
                self._kafka_publisher is not None and self._kafka_publisher.pending_count >= 0
            ),
        )
        self._health_server.set_ready(True)

        self._running = True
        logger.info(
            "Data Ingestion Service started — " "streaming live data for %d connector(s)",
            sum(1 for c in self._connectors if c.is_connected),
        )

        # Hold until SIGTERM/SIGINT
        await self._shutdown_event.wait()

    async def stop(self) -> None:
        """
        Gracefully stop the data ingestion service.

        Disconnects all connectors, drains the Kafka publisher (waits for
        all in-flight deliveries to complete), and flushes S3/DynamoDB buffers.
        """
        if not self._running:
            return

        logger.info("Stopping Data Ingestion Service")
        self._running = False

        # Signal load balancer / ASG to stop routing to this instance
        # before disconnecting — gives in-flight requests time to drain.
        self._health_server.set_ready(False)

        # Stop producer heartbeat writer — no more DynamoDB writes after shutdown
        if self._producer_heartbeat_task is not None:
            self._producer_heartbeat_task.cancel()
            try:
                await self._producer_heartbeat_task
            except asyncio.CancelledError:
                pass

        # Stop governor watchdog first so it doesn't restart the governor after we stop it
        if self._governor_watchdog_task is not None:
            self._governor_watchdog_task.cancel()
            try:
                await self._governor_watchdog_task
            except asyncio.CancelledError:
                pass

        # Stop candle stream watchdog before the stream so it can't restart a dying stream
        if self._candle_stream_watchdog_task is not None:
            self._candle_stream_watchdog_task.cancel()
            try:
                await self._candle_stream_watchdog_task
            except asyncio.CancelledError:
                pass

        # Stop Phase 5 candle stream (graceful — stops the fetch loop)
        if self._candle_stream is not None:
            await self._candle_stream.stop()
        if self._candle_stream_task is not None:
            self._candle_stream_task.cancel()
            try:
                await self._candle_stream_task
            except asyncio.CancelledError:
                pass

        # Stop Phase 5 market-phase governor (stops the IST-clock background loop)
        if self._phase_governor is not None:
            await self._phase_governor.stop()
        if self._phase_governor_task is not None:
            self._phase_governor_task.cancel()
            try:
                await self._phase_governor_task
            except asyncio.CancelledError:
                pass

        # Disconnect broker WebSocket connectors
        for connector in self._connectors:
            try:
                await connector.disconnect()
            except Exception:
                logger.exception("Error disconnecting %s", connector.__class__.__name__)

        # Stop Kafka publisher — drains all in-flight deliveries before returning.
        # KafkaTickPublisher.stop() calls producer.flush(timeout=10) so we never
        # drop confirmed-produced messages on an orderly shutdown.
        if self._kafka_publisher is not None:
            await self._kafka_publisher.stop()

        # Flush S3 and DynamoDB write buffers
        if self._s3_writer is not None:
            await self._s3_writer.flush()
        if self._dynamo_writer is not None:
            await self._dynamo_writer.flush()

        await self._health_server.stop()
        self._shutdown_event.set()
        logger.info("Data Ingestion Service stopped")

    # ── Phase 5: Feature pipeline ─────────────────────────────────────────────

    async def _governor_watchdog(self) -> None:
        """
        Restart the market phase governor task if it exits unexpectedly.

        The governor's run() loop has no restart logic of its own. If it is
        cancelled or raises, phase transitions stop firing and the candle stream
        gets stuck in whatever phase it last received. This watchdog detects that
        condition and restarts the governor task so phases keep advancing.

        Checks every 15 seconds. Restarts are only attempted while the service
        is still running (self._running) and the governor object exists.
        """
        _WATCHDOG_INTERVAL = 15.0
        while self._running:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL)
            except asyncio.CancelledError:
                break
            if not self._running or self._phase_governor is None:
                break
            task = self._phase_governor_task
            if task is not None and task.done() and not task.cancelled():
                exc = task.exception()
                logger.critical(
                    "governor_watchdog.task_died_with_exception",
                    exc=str(exc),
                    detail="Governor task raised — restarting",
                )
            elif task is not None and task.cancelled():
                logger.critical(
                    "governor_watchdog.task_cancelled",
                    detail="Governor task was cancelled unexpectedly — restarting",
                )
            else:
                continue  # task is still running — nothing to do
            # Restart the governor
            self._phase_governor_task = asyncio.create_task(
                self._phase_governor.run(),
                name="phase_governor",
            )
            logger.info(
                "governor_watchdog.restarted",
                current_phase=self._phase_governor.current_phase().value,
            )

    async def _candle_stream_watchdog(self) -> None:
        """
        Restart the candle stream task if it exits unexpectedly.

        ``CancelledError`` bypasses the ``except Exception`` handler inside
        ``_stream_loop``, so if the task is cancelled externally (e.g. by a
        stray task.cancel() call) it dies silently.  This watchdog detects that
        condition every 30 seconds and restarts the stream so candles keep
        flowing even after a Zerodha WebSocket drop or transient API failure.
        """
        _WATCHDOG_INTERVAL = 30.0
        while self._running:
            try:
                await asyncio.sleep(_WATCHDOG_INTERVAL)
            except asyncio.CancelledError:
                break
            if not self._running or self._candle_stream is None:
                break
            task = self._candle_stream_task
            if task is None or not task.done():
                continue  # still running — nothing to do
            reason = "cancelled" if task.cancelled() else str(task.exception())
            logger.critical(
                "candle_stream_watchdog.task_dead",
                reason=reason,
                detail="Candle stream task died — restarting",
            )
            self._candle_stream_task = asyncio.create_task(
                self._candle_stream.start(),
                name="candle_stream",
            )
            logger.info("candle_stream_watchdog.restarted")

    async def _producer_heartbeat_loop(self) -> None:
        """
        Write a DynamoDB heartbeat key every 10s while the service is running.

        Key: ``PK=HEARTBEAT#{market} SK=CURRENT updated_at=<iso>`` in the
        latest-prices table.  risk_engine's ``_monitor_producer_heartbeat()``
        reads this to detect a dead WebSocket feed independently of Kafka
        consumer lag (ADR-021 Phase 1).

        Uses the DynamoDB writer client already established at startup.
        Failures are logged but never crash the loop — a missed write is
        silently ignored by risk_engine until the timeout expires.
        """
        import boto3  # noqa: PLC0415
        from datetime import datetime, timezone as _tz  # noqa: PLC0415

        _INTERVAL = 10.0  # seconds between heartbeat writes
        _TABLE = self._settings.aws.dynamodb_table_prices
        _MARKET = getattr(self._settings, "market", "NSE").upper()

        dynamo = boto3.client("dynamodb", region_name=self._settings.aws.region)
        logger.info(
            "producer_heartbeat_loop.started market=%s table=%s interval=%.0fs",
            _MARKET, _TABLE, _INTERVAL,
        )

        while self._running:
            try:
                await asyncio.sleep(_INTERVAL)
                if not self._running:
                    break

                now_iso = datetime.now(_tz.utc).isoformat()
                await asyncio.to_thread(
                    dynamo.put_item,
                    TableName=_TABLE,
                    Item={
                        "PK": {"S": f"HEARTBEAT#{_MARKET}"},
                        "SK": {"S": "CURRENT"},
                        "updated_at": {"S": now_iso},
                        "market": {"S": _MARKET},
                    },
                )
            except asyncio.CancelledError:
                break
            except Exception:
                logger.warning("producer_heartbeat_loop.write_failed — will retry", exc_info=True)

        logger.info("producer_heartbeat_loop.stopped market=%s", _MARKET)

    async def _setup_feature_pipeline(self) -> None:
        """
        Instantiate and wire the Phase 5 feature pipeline.

        Components:
          - ``FeatureEngine``          — pure stateless feature computation.
          - ``FeatureWriter``          — dual DynamoDB write (LATEST + CANDLE#ts).
          - ``FeatureArchiver``        — POST_CLOSE S3 parquet export.
          - ``MarketPhaseGovernor``    — routes phase-change events to archiver.
          - ``IntradayCandleStream``   — REST candle fetcher; calls feature hook
                                        on every confirmed candle.

        All failures are non-fatal: the exception is logged and the rest of
        the data-ingestion service continues without features.
        """
        try:
            import boto3  # type: ignore[import]
            from data_ingestion.candle_stream import IntradayCandleStream
            from data_ingestion.features.feature_archiver import FeatureArchiver
            from data_ingestion.features.feature_engine import FeatureEngine
            from data_ingestion.features.feature_writer import FeatureWriter
            from shared.zerodha.market_phase import MarketPhaseGovernor

            # ── Shared boto3 clients ─────────────────────────────────────────
            dynamo_client = boto3.client("dynamodb", region_name=self._settings.aws.region)
            s3_client = boto3.client("s3", region_name=self._settings.aws.region)

            # ── Feature components ───────────────────────────────────────────
            feature_engine = FeatureEngine()
            # Fix: constructor param is `features_table`, not `table_name`
            feature_writer = FeatureWriter(
                dynamo_client=dynamo_client,
                features_table=self._settings.aws.dynamodb_table_features,
            )

            # ── NSE symbol list for FeatureArchiver ──────────────────────────
            nse_symbols: list[str] = []
            # instrument_tokens maps tradingsymbol → Zerodha numeric token.
            # Populated at startup via ZerodhaBrokerClient.get_instrument_tokens()
            # scoped to the symbols returned by InstrumentLoader.  An empty map
            # causes the candle stream to be skipped entirely (fail-closed).
            instrument_tokens: dict[str, int] = {}
            try:
                from strategy_engine.universe.instrument_loader import (
                    InstrumentLoader,
                )

                loader = InstrumentLoader(config_path=self._instruments_config_path)
                loader.load()
                nse_symbols = loader.get_all_symbols("NSE")
            except Exception:
                logger.warning(
                    "feature_pipeline.symbol_load_failed",
                    detail=(
                        "Could not load NSE symbols for FeatureArchiver "
                        "— archive will run with 0 symbols"
                    ),
                )

            # ── FeatureArchiver ──────────────────────────────────────────────
            self._feature_archiver = FeatureArchiver(
                dynamo_client=dynamo_client,
                s3_client=s3_client,
                features_table=self._settings.aws.dynamodb_table_features,
                data_bucket=self._settings.aws.s3_bucket,
                symbols=nse_symbols,
            )

            # ── MarketPhaseGovernor — create + register archiver listener ────
            # NOTE: the governor's background loop (governor.run()) is started
            # AFTER the candle stream is created and has registered its own
            # listener (via phase_governor= in the constructor).  This ensures
            # the initial phase broadcast that run() fires immediately on startup
            # reaches BOTH the archiver AND the candle stream.  Starting the
            # task here (before the stream exists) would cause the stream to miss
            # the initial phase and remain stuck in POST_CLOSE indefinitely.
            self._phase_governor = MarketPhaseGovernor()
            self._phase_governor.add_listener(self._feature_archiver.on_phase_change)

            # ── IntradayCandleStream (requires ZerodhaBrokerClient) ──────────
            # ZerodhaBrokerClient lives in execution_engine; import lazily so
            # data_ingestion does not have a hard compile-time dependency on it.
            # If the import fails, the candle stream is skipped but features can
            # still be wired later when the broker client is moved to shared/.
            zerodha_broker: Any = None
            try:
                from execution_engine.brokers.zerodha_broker import (  # type: ignore[import]
                    ZerodhaBrokerClient,
                )

                zerodha_broker = ZerodhaBrokerClient(settings=self._settings, dynamo_client=dynamo_client)
                await zerodha_broker.connect()
            except ImportError:
                logger.warning(
                    "feature_pipeline.zerodha_broker_unavailable",
                    detail=(
                        "ZerodhaBrokerClient not importable from execution_engine "
                        "— candle stream disabled. Move to shared/ in Phase 7."
                    ),
                )
            except Exception:
                logger.exception(
                    "feature_pipeline.zerodha_broker_connect_failed",
                    detail="ZerodhaBrokerClient.connect() failed — candle stream disabled",
                )

            if zerodha_broker is not None:
                # ── Resolve Zerodha instrument tokens ────────────────────────
                # kite.instruments("NSE") maps tradingsymbol → numeric token.
                # IntradayCandleStream builds its round-robin fetch queue from
                # this dict; an empty map produces zero candles and zero features.
                # IMPORTANT: always pass symbols=set(nse_symbols) — never None.
                # Passing None tells get_instrument_tokens() to return ALL ~1800
                # NSE instruments, silently expanding the candle stream beyond our
                # configured trading universe.  If InstrumentLoader failed and
                # nse_symbols is empty, we skip the candle stream entirely rather
                # than start it with the full NSE universe.
                if not nse_symbols:
                    logger.warning(
                        "feature_pipeline.no_symbols_skip_candle_stream",
                        detail=(
                            "InstrumentLoader returned zero NSE symbols — "
                            "candle stream skipped. Features will not be generated. "
                            "Fix instruments.yaml or InstrumentLoader config."
                        ),
                    )
                else:
                    try:
                        instrument_tokens = await zerodha_broker.get_instrument_tokens(
                            exchange="NSE",
                            symbols=set(nse_symbols),  # always scoped to our universe
                        )
                        logger.info(
                            "feature_pipeline.tokens_resolved",
                            symbol_count=len(instrument_tokens),
                        )
                    except Exception:
                        logger.exception(
                            "feature_pipeline.token_resolution_failed",
                            detail=(
                                "get_instrument_tokens() failed — candle stream "
                                "skipped. Features will not be generated."
                            ),
                        )
                        instrument_tokens = {}  # stays empty — skip stream below

                    if instrument_tokens:
                        strategy_cfg = getattr(self._settings, "strategy", None)
                        candle_intervals = getattr(
                            strategy_cfg,
                            "candle_intervals",
                            ["minute", "5minute", "15minute"],
                        )
                        self._candle_stream = IntradayCandleStream(
                            zerodha=zerodha_broker,
                            instrument_tokens=instrument_tokens,
                            dynamo_client=dynamo_client,
                            candle_table=(
                                f"{self._settings.aws.dynamodb_table_prefix}-candle-cache"
                            ),
                            phase_governor=self._phase_governor,
                            intervals=candle_intervals,
                            feature_engine=feature_engine,
                            feature_writer=feature_writer,
                        )
                        # Launch as a fire-and-forget background task alongside the
                        # main service loop — does not block start() from completing.
                        self._candle_stream_task = asyncio.create_task(
                            self._candle_stream.start(),
                            name="candle_stream",
                        )
                        self._candle_stream_watchdog_task = asyncio.create_task(
                            self._candle_stream_watchdog(),
                            name="candle_stream_watchdog",
                        )
                    else:
                        logger.warning(
                            "feature_pipeline.empty_token_map_skip_candle_stream",
                            detail=(
                                "get_instrument_tokens() returned empty map for "
                                f"{len(nse_symbols)} NSE symbols — candle stream skipped."
                            ),
                        )

            # ── Start governor background loop ───────────────────────────────
            # Both the archiver AND the candle stream (if created above) are now
            # registered as listeners.  Starting the task here guarantees that
            # the initial phase broadcast from run() reaches all registered
            # listeners — including the candle stream which registered itself
            # during IntradayCandleStream.__init__(phase_governor=...).
            self._phase_governor_task = asyncio.create_task(
                self._phase_governor.run(),
                name="phase_governor",
            )
            self._governor_watchdog_task = asyncio.create_task(
                self._governor_watchdog(),
                name="governor_watchdog",
            )

            logger.info(
                "feature_pipeline.started",
                nse_symbols=len(nse_symbols),
                candle_stream_active=self._candle_stream is not None,
                archiver_active=self._feature_archiver is not None,
            )

        except Exception:
            logger.exception(
                "feature_pipeline.setup_failed",
                detail=(
                    "Feature pipeline failed to initialize — service continues without features"
                ),
            )

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
        us_symbols = loader.get_all_symbols("US")

        if nse_symbols and self._connectors:
            zerodha_connector = next(
                (c for c in self._connectors if isinstance(c, ZerodhaConnector)),
                None,
            )
            if zerodha_connector and zerodha_connector.is_connected:
                try:
                    await zerodha_connector.subscribe(nse_symbols)
                except Exception:
                    logger.exception("Zerodha subscription failed for symbols: %s", nse_symbols)
            else:
                logger.warning("Zerodha connector is not connected — NSE subscription skipped")

        if us_symbols and self._connectors:
            alpaca_connector = next(
                (c for c in self._connectors if isinstance(c, AlpacaConnector)),
                None,
            )
            if alpaca_connector and alpaca_connector.is_connected:
                try:
                    await alpaca_connector.subscribe(us_symbols)
                except Exception:
                    logger.exception("Alpaca subscription failed for symbols: %s", us_symbols)
            else:
                logger.warning("Alpaca connector is not connected — US subscription skipped")

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
        us_raw = _os.environ.get("SUBSCRIBE_US_SYMBOLS", "")

        nse_symbols = [s.strip() for s in nse_raw.split(",") if s.strip()]
        us_symbols = [s.strip() for s in us_raw.split(",") if s.strip()]

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
