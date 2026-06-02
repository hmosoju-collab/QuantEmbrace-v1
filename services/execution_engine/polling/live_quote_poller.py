"""
Live Quote Poller — Batch bid/ask quotes via ``kite.quote()``.

Fetches live market depth for the entire instrument watchlist in a single
API call every 2 seconds.  One ``kite.quote([50 instruments])`` call returns
bid, ask, LTP, volume, circuit limits, and best-5 depth for all 50 instruments.

Rate cost: 0.5 req/sec (1 call / 2000ms) in NORMAL phase.

Why this matters:
-----------------
Zerodha WebSocket (KiteTicker) gives LTP only.  ``kite.quote()`` adds:
  - **bid/ask spread**: BUY into a 1% spread on 1000 shares costs ₹10/share
    in slippage.  The spread gate rejects entries when spread > threshold.
  - **circuit limits**: If an instrument is in circuit (upper/lower lock),
    orders will be rejected by the exchange.  Pre-checking avoids wasted
    API calls and error noise.
  - **depth (5-level order book)**: Used by risk engine to estimate market
    impact for larger orders.

Spread gate integration:
    ``LiveQuotePoller`` maintains a thread-safe quote cache.  The risk engine
    calls ``get_spread_bps(symbol)`` before approving a signal — if the spread
    exceeds ``max_spread_bps``, the signal is rejected at validation time with
    reason ``spread_too_wide``.  This prevents slippage-heavy entries during
    low-liquidity periods.

Phase awareness:
    MARKET_OPEN  — enabled at the configured batch interval so ORB entries
                   still get live spread/circuit checks.
    PRE_CLOSE    — enabled so pre-close strategy approvals are not blind to
                   spread/circuit state.
    POST_CLOSE   — disabled.

Usage::

    poller = LiveQuotePoller(
        zerodha=zerodha_client,
        instruments=["NSE:RELIANCE", "NSE:INFY", ...],
        rate_limiter=rate_limiter,
        phase_governor=governor,
        max_spread_bps=50,        # reject signals when spread > 50 bps
    )
    asyncio.create_task(poller.start())

    # From risk engine spread validator:
    spread = poller.get_spread_bps("NSE:RELIANCE")
    if spread is not None and spread > threshold:
        return RiskDecision.reject("spread_too_wide", spread_bps=spread)
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Any, Optional

from shared.config.settings import AppSettings, get_settings
from shared.logging.logger import get_logger
from shared.zerodha.market_phase import MarketPhase, MarketPhaseGovernor
from shared.zerodha.rate_limiter import EndpointClass, Priority, ZerodhaRateLimiter

from execution_engine.brokers.zerodha_broker import ZerodhaBrokerClient

logger = get_logger(__name__, service_name="execution_engine")

# ── Phase configuration ────────────────────────────────────────────────────────

# Phases where live quote polling is active.
_ACTIVE_PHASES: frozenset[MarketPhase] = frozenset({
    MarketPhase.PRE_AUCTION,
    MarketPhase.MARKET_OPEN,
    MarketPhase.NORMAL,
    MarketPhase.PRE_CLOSE,
    MarketPhase.CLOSING,
})

_DISABLED_PHASES: frozenset[MarketPhase] = frozenset({
    MarketPhase.PRE_OPEN,
    MarketPhase.POST_CLOSE,
})

_POLL_INTERVAL_SECONDS: float = 2.0      # 0.5 req/sec
_IDLE_INTERVAL_SECONDS: float = 10.0     # disabled phase — minimal heartbeat check
_MAX_CONSECUTIVE_ERRORS: int = 10


class QuoteSnapshot:
    """Immutable quote snapshot for one instrument."""

    __slots__ = (
        "instrument", "ltp", "bid", "ask", "spread_bps",
        "circuit_lower", "circuit_upper", "volume",
        "depth_bids", "depth_asks", "captured_at", "captured_at_utc",
    )

    def __init__(
        self,
        instrument: str,
        ltp: float,
        bid: float,
        ask: float,
        circuit_lower: float,
        circuit_upper: float,
        volume: int,
        depth_bids: list[dict],
        depth_asks: list[dict],
        captured_at: float,
        captured_at_utc: Optional[datetime] = None,
    ) -> None:
        self.instrument      = instrument
        self.ltp             = ltp
        self.bid             = bid
        self.ask             = ask
        self.circuit_lower   = circuit_lower
        self.circuit_upper   = circuit_upper
        self.volume          = volume
        self.depth_bids      = depth_bids
        self.depth_asks      = depth_asks
        self.captured_at     = captured_at
        self.captured_at_utc = captured_at_utc or datetime.now(timezone.utc)
        # Spread in basis points: (ask - bid) / mid × 10000
        mid = (bid + ask) / 2 if (bid + ask) > 0 else ltp
        self.spread_bps = round((ask - bid) / mid * 10_000, 2) if mid > 0 else None

    def is_stale(self, max_age_seconds: float = 5.0) -> bool:
        """Return True if snapshot is older than ``max_age_seconds``."""
        return (time.monotonic() - self.captured_at) > max_age_seconds

    def is_circuit_locked(self) -> bool:
        """True if price is at circuit limit (orders will be rejected by exchange)."""
        if self.ltp <= 0:
            return False
        if self.circuit_upper > 0 and self.ltp >= self.circuit_upper:
            return True
        if self.circuit_lower > 0 and self.ltp <= self.circuit_lower:
            return True
        return False


class LiveQuotePoller:
    """
    Batch live quote fetcher for the full instrument watchlist.

    Maintains a thread-safe in-memory quote cache keyed by
    ``"{EXCHANGE}:{SYMBOL}"`` strings.  Consumers (risk engine, execution
    engine limit re-pricer) call ``get_quote()`` or ``get_spread_bps()``
    for zero-latency reads from the cache.

    Args:
        zerodha:       Connected Zerodha broker client.
        instruments:   List of ``"{EXCHANGE}:{SYMBOL}"`` strings to poll.
                       Maximum 500 per Kite API call — split if needed.
        rate_limiter:  Shared ``ZerodhaRateLimiter``.
        phase_governor: Optional phase governor for automatic enable/disable.
        max_spread_bps: Spread threshold above which instruments are flagged.
                        Logged as WARNING; used by spread gate in risk engine.
        settings:      App settings.
    """

    def __init__(
        self,
        zerodha: ZerodhaBrokerClient,
        instruments: list[str],
        rate_limiter: ZerodhaRateLimiter,
        phase_governor: Optional[MarketPhaseGovernor] = None,
        max_spread_bps: float = 50.0,
        dynamo_client: Optional[Any] = None,
        prices_table: Optional[str] = None,
        settings: Optional[AppSettings] = None,
    ) -> None:
        self._zerodha       = zerodha
        self._instruments   = list(instruments)
        self._rate_limiter  = rate_limiter
        self._max_spread_bps = max_spread_bps
        self._settings      = settings or get_settings()
        self._phase         = MarketPhase.POST_CLOSE
        self._running       = False
        self._consecutive_errors = 0

        # DynamoDB client for writing QUOTE#{market}#{symbol}/LATEST so the
        # risk engine's RiskContextBuilder._fetch_live_spread_bps() returns
        # real data instead of None.  Optional — poller works without it
        # (in-memory cache still maintained).
        self._dynamo = dynamo_client
        self._prices_table = prices_table or self._settings.aws.dynamodb_table_prices

        # Quote cache: instrument_key → QuoteSnapshot
        self._quotes: dict[str, QuoteSnapshot] = {}
        self._lock = asyncio.Lock()

        if phase_governor is not None:
            phase_governor.add_listener(self.on_phase_change)

    # ── Phase awareness ────────────────────────────────────────────────────────

    def on_phase_change(self, phase_name: str) -> None:
        """Called by ``MarketPhaseGovernor`` on phase transitions."""
        try:
            new_phase = MarketPhase(phase_name)
            was_active = self._phase in _ACTIVE_PHASES
            is_active  = new_phase in _ACTIVE_PHASES
            self._phase = new_phase
            if was_active and not is_active:
                logger.info(
                    "live_quote_poller.disabled",
                    phase=phase_name,
                    reason="outside live quote polling window",
                )
            elif not was_active and is_active:
                logger.info("live_quote_poller.enabled", phase=phase_name)
        except ValueError:
            logger.warning("live_quote_poller.unknown_phase", phase_name=phase_name)

    @property
    def is_active(self) -> bool:
        """True when the current phase allows quote polling."""
        return self._phase in _ACTIVE_PHASES

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Start the quote polling loop."""
        self._running = True
        logger.info(
            "live_quote_poller.started",
            instrument_count=len(self._instruments),
            poll_interval_s=_POLL_INTERVAL_SECONDS,
            max_spread_bps=self._max_spread_bps,
        )
        await self._poll_loop()

    async def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        logger.info("live_quote_poller.stopped")

    # ── Consumer API ──────────────────────────────────────────────────────────

    def get_quote(self, instrument: str) -> Optional[QuoteSnapshot]:
        """
        Return the latest ``QuoteSnapshot`` for an instrument.

        Args:
            instrument: ``"{EXCHANGE}:{SYMBOL}"`` e.g. ``"NSE:RELIANCE"``.

        Returns:
            ``QuoteSnapshot`` or None if not yet fetched.
        """
        return self._quotes.get(instrument)

    def get_spread_bps(self, instrument: str) -> Optional[float]:
        """
        Return the current bid-ask spread in basis points.

        Returns:
            Float spread in bps, or None if quote is unavailable or stale.
        """
        snap = self._quotes.get(instrument)
        if snap is None or snap.is_stale():
            return None
        return snap.spread_bps

    def get_ltp(self, instrument: str) -> Optional[float]:
        """Return the last traded price from the quote cache."""
        snap = self._quotes.get(instrument)
        if snap is None or snap.is_stale():
            return None
        return snap.ltp

    def is_circuit_locked(self, instrument: str) -> bool:
        """True if the instrument is at a circuit limit (orders will fail)."""
        snap = self._quotes.get(instrument)
        if snap is None or snap.is_stale():
            return False
        return snap.is_circuit_locked()

    def get_all_quotes(self) -> dict[str, QuoteSnapshot]:
        """Return a shallow copy of the full quote cache."""
        return dict(self._quotes)

    # ── Main loop ─────────────────────────────────────────────────────────────

    async def _poll_loop(self) -> None:
        while self._running:
            if not self.is_active:
                # Phase disabled — sleep longer, skip API call
                await asyncio.sleep(_IDLE_INTERVAL_SECONDS)
                continue

            cycle_start = asyncio.get_event_loop().time()

            try:
                await self._poll_cycle()
                self._consecutive_errors = 0
            except Exception:
                self._consecutive_errors += 1
                logger.exception(
                    "live_quote_poller.cycle_error",
                    consecutive_errors=self._consecutive_errors,
                )
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.critical(
                        "live_quote_poller.stale_quotes",
                        message=(
                            "Quote cache is stale — spread gate will pass all instruments "
                            "until quotes are refreshed."
                        ),
                    )

            elapsed    = asyncio.get_event_loop().time() - cycle_start
            sleep_time = max(0.0, _POLL_INTERVAL_SECONDS - elapsed)
            if sleep_time > 0:
                await asyncio.sleep(sleep_time)

    # ── Single poll cycle ─────────────────────────────────────────────────────

    async def _poll_cycle(self) -> None:
        """
        Fetch quotes for all instruments in one API call, update cache.

        Kite supports up to 500 instruments per ``quote()`` call.  If
        ``self._instruments`` exceeds 500, split into batches.  In practice
        our watchlist is ≤50 so this is one call.
        """
        if not self._instruments:
            return

        await self._rate_limiter.acquire(Priority.MEDIUM, EndpointClass.QUOTE)
        raw_quotes = await self._zerodha.get_batch_quotes(self._instruments)

        if not raw_quotes:
            return

        captured_at = time.monotonic()
        new_quotes: dict[str, QuoteSnapshot] = {}
        wide_spread_instruments: list[str] = []

        for instrument, data in raw_quotes.items():
            try:
                depth = data.get("depth", {})
                bids  = depth.get("buy", [])
                asks  = depth.get("sell", [])

                snap = QuoteSnapshot(
                    instrument    = instrument,
                    ltp           = float(data.get("last_price", 0)),
                    bid           = float(bids[0]["price"]) if bids else 0.0,
                    ask           = float(asks[0]["price"]) if asks else 0.0,
                    circuit_lower = float(data.get("lower_circuit_limit", 0)),
                    circuit_upper = float(data.get("upper_circuit_limit", 0)),
                    volume        = int(data.get("volume", 0)),
                    depth_bids    = bids[:5],
                    depth_asks    = asks[:5],
                    captured_at   = captured_at,
                )
                new_quotes[instrument] = snap

                # Flag wide spreads
                if (
                    snap.spread_bps is not None
                    and snap.spread_bps > self._max_spread_bps
                ):
                    wide_spread_instruments.append(instrument)

                # Flag circuit-locked instruments
                if snap.is_circuit_locked():
                    logger.warning(
                        "live_quote_poller.circuit_locked",
                        instrument=instrument,
                        ltp=snap.ltp,
                        circuit_lower=snap.circuit_lower,
                        circuit_upper=snap.circuit_upper,
                    )

            except Exception:
                logger.exception(
                    "live_quote_poller.parse_error",
                    instrument=instrument,
                )

        # Atomic cache update
        async with self._lock:
            self._quotes.update(new_quotes)

        if wide_spread_instruments:
            logger.warning(
                "live_quote_poller.wide_spreads",
                instruments=wide_spread_instruments,
                threshold_bps=self._max_spread_bps,
            )

        logger.debug(
            "live_quote_poller.cycle_complete",
            updated=len(new_quotes),
            wide_spread_count=len(wide_spread_instruments),
            phase=self._phase.value,
        )

        # Persist quotes to DynamoDB so RiskContextBuilder can read them.
        # Failures are non-fatal — in-memory cache remains authoritative.
        if self._dynamo is not None and new_quotes:
            await self._write_quotes_to_dynamo(new_quotes)

    # ── DynamoDB write ────────────────────────────────────────────────────────

    async def _write_quotes_to_dynamo(
        self, quotes: dict[str, QuoteSnapshot]
    ) -> None:
        """
        Persist all updated quotes to DynamoDB concurrently.

        Writes each quote as:
            PK = QUOTE#{market}#{symbol}   (e.g. QUOTE#NSE#RELIANCE)
            SK = LATEST

        Fields written:
            spread_bps   (N)  — basis points; None → attribute omitted
            bid          (N)
            ask          (N)
            ltp          (N)
            volume       (N)
            captured_at  (S)  — ISO 8601 UTC, e.g. "2026-05-06T07:30:00.123456+00:00"

        These are read by:
            ``risk_engine.context.risk_context_builder.RiskContextBuilder``
                ``._fetch_live_spread_bps(market, symbol)``

        DynamoDB key format expected by RiskContextBuilder:
            PK = QUOTE#{market}#{symbol}  (market from Signal.market, symbol from Signal.symbol)
            SK = LATEST

        Args:
            quotes: Snapshot dict from the current poll cycle.
        """

        async def _put_one(instrument: str, snap: QuoteSnapshot) -> None:
            # instrument is "{EXCHANGE}:{SYMBOL}" e.g. "NSE:RELIANCE"
            parts = instrument.split(":", 1)
            if len(parts) != 2:
                return
            market, symbol = parts[0].upper(), parts[1].upper()

            item: dict[str, Any] = {
                "PK":          {"S": f"QUOTE#{market}#{symbol}"},
                "SK":          {"S": "LATEST"},
                "bid":         {"N": str(snap.bid)},
                "ask":         {"N": str(snap.ask)},
                "ltp":         {"N": str(snap.ltp)},
                "volume":      {"N": str(snap.volume)},
                "captured_at": {"S": snap.captured_at_utc.isoformat()},
            }
            if snap.spread_bps is not None:
                item["spread_bps"] = {"N": str(snap.spread_bps)}

            try:
                await asyncio.to_thread(
                    self._dynamo.put_item,
                    TableName=self._prices_table,
                    Item=item,
                )
            except Exception:
                logger.warning(
                    "live_quote_poller.dynamo_write_failed",
                    instrument=instrument,
                )

        await asyncio.gather(*(_put_one(k, v) for k, v in quotes.items()))
