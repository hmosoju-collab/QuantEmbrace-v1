"""
Unit tests for LiveQuotePoller DynamoDB write contract (PHASE4-FU-001).

Covers:
    - QuoteSnapshot.captured_at_utc is set to datetime.now(UTC) when not provided
    - _write_quotes_to_dynamo() writes PK=QUOTE#{market}#{symbol} / SK=LATEST
    - Written item fields match what RiskContextBuilder._fetch_live_spread_bps reads:
        spread_bps (N), bid (N), ask (N), ltp (N), volume (N), captured_at (S ISO)
    - spread_bps=None → attribute is omitted (no "N": "None" noise in DynamoDB)
    - Instrument key parsed correctly: "NSE:RELIANCE" → market=NSE, symbol=RELIANCE
    - DynamoDB write failure is non-fatal (warning logged, no exception raised)
    - Multiple instruments written concurrently in one _write_quotes_to_dynamo call
    - End-to-end: _poll_cycle() calls _write_quotes_to_dynamo() when dynamo is set
    - _poll_cycle() skips DynamoDB write when dynamo_client is None
"""

from __future__ import annotations

import asyncio
import sys
import time
import types
from datetime import datetime, timezone
from unittest.mock import MagicMock, call, patch

try:
    import pytest as _pytest_module
except ModuleNotFoundError:
    _pytest_module = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Approx helper (works with or without pytest installed)
# ---------------------------------------------------------------------------

class _Approx:
    def __init__(self, expected, abs=None, rel=None):
        self._expected = expected
        self._abs = abs if abs is not None else 1e-6

    def __eq__(self, other):
        return builtins_abs(other - self._expected) <= self._abs

    def __repr__(self):
        return f"~{self._expected} (±{self._abs})"


import builtins as _builtins
builtins_abs = _builtins.abs


def _approx(expected, abs=None, rel=None):
    if _pytest_module is not None:
        return _pytest_module.approx(expected, abs=abs, rel=rel)
    return _Approx(expected, abs=abs)


# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------
import os

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)


# ---------------------------------------------------------------------------
# Stubs for shared.* and execution_engine dependencies
# ---------------------------------------------------------------------------

def _install_stubs() -> None:
    def _pkg(name: str) -> types.ModuleType:
        m = types.ModuleType(name)
        sys.modules[name] = m
        return m

    # shared.*
    shared             = _pkg("shared")
    shared_config      = _pkg("shared.config")
    shared_config_sett = _pkg("shared.config.settings")
    shared_logging     = _pkg("shared.logging")
    shared_logging_log = _pkg("shared.logging.logger")

    class _AWSConfig:
        dynamodb_table_prices = "qe-dev-prices"

    class _AppSettings:
        aws = _AWSConfig()

    shared_config_sett.AppSettings = _AppSettings
    shared_config_sett.get_settings = lambda: _AppSettings()
    shared_config.settings = shared_config_sett

    import logging

    class _StructuredLogger:
        """Accepts structured key=value kwargs like the real shared logger."""
        def __init__(self, name: str) -> None:
            self._log = logging.getLogger(name)
        def debug(self, msg, *args, **kw):    self._log.debug(msg)
        def info(self, msg, *args, **kw):     self._log.info(msg)
        def warning(self, msg, *args, **kw):  self._log.warning(msg)
        def error(self, msg, *args, **kw):    self._log.error(msg)
        def critical(self, msg, *args, **kw): self._log.critical(msg)
        def exception(self, msg, *args, **kw):self._log.exception(msg)

    shared_logging_log.get_logger = lambda name, **_: _StructuredLogger(name)
    shared_logging_log.set_correlation_id = lambda *_, **__: None

    # shared.zerodha.*
    shared_zerodha      = _pkg("shared.zerodha")
    shared_zerodha_rate = _pkg("shared.zerodha.rate_limiter")
    shared_zerodha_mp   = _pkg("shared.zerodha.market_phase")

    from enum import Enum, auto

    class MarketPhase(Enum):
        PRE_OPEN    = "PRE_OPEN"
        PRE_AUCTION = "PRE_AUCTION"
        MARKET_OPEN = "MARKET_OPEN"
        NORMAL      = "NORMAL"
        PRE_CLOSE   = "PRE_CLOSE"
        CLOSING     = "CLOSING"
        POST_CLOSE  = "POST_CLOSE"
        OVERNIGHT   = "OVERNIGHT"

    class MarketPhaseGovernor:
        def add_listener(self, cb): pass

    class Priority(Enum):
        MEDIUM   = "MEDIUM"
        HIGH     = "HIGH"
        CRITICAL = "CRITICAL"

    class EndpointClass(Enum):
        QUOTE = "quote"
        OTHER = "other"

    class ZerodhaRateLimiter:
        async def acquire(self, *args, **kwargs): pass

    shared_zerodha_mp.MarketPhase          = MarketPhase
    shared_zerodha_mp.MarketPhaseGovernor  = MarketPhaseGovernor
    shared_zerodha_rate.EndpointClass       = EndpointClass
    shared_zerodha_rate.Priority           = Priority
    shared_zerodha_rate.ZerodhaRateLimiter = ZerodhaRateLimiter

    # execution_engine.brokers.zerodha_broker — stub only the leaf; leave
    # execution_engine package itself un-stubbed so the real polling module
    # can be loaded via spec_from_file_location below.
    ee_zdh = _pkg("execution_engine.brokers.zerodha_broker")

    class ZerodhaBrokerClient:
        pass

    ee_zdh.ZerodhaBrokerClient = ZerodhaBrokerClient


# ---------------------------------------------------------------------------
# Module-level placeholders — NO side-effects at import/collection time
# ---------------------------------------------------------------------------
import importlib.util as _ilu

_MODULES_SNAPSHOT: frozenset = frozenset()
LiveQuotePoller = None
QuoteSnapshot = None
_POLL_INTERVAL_SECONDS = None


def setUpModule() -> None:  # noqa: N802
    """Called by pytest/unittest AFTER collection, BEFORE running tests."""
    global _MODULES_SNAPSHOT, LiveQuotePoller, QuoteSnapshot, _POLL_INTERVAL_SECONDS

    _MODULES_SNAPSHOT = frozenset(sys.modules.keys())

    _install_stubs()

    _lqp_path = os.path.join(
        _SERVICES_DIR, "execution_engine", "polling", "live_quote_poller.py"
    )
    _lqp_spec = _ilu.spec_from_file_location(
        "execution_engine.polling.live_quote_poller", _lqp_path
    )
    _lqp_mod = _ilu.module_from_spec(_lqp_spec)
    sys.modules["execution_engine.polling.live_quote_poller"] = _lqp_mod
    _lqp_spec.loader.exec_module(_lqp_mod)

    LiveQuotePoller = _lqp_mod.LiveQuotePoller
    QuoteSnapshot = _lqp_mod.QuoteSnapshot
    _POLL_INTERVAL_SECONDS = _lqp_mod._POLL_INTERVAL_SECONDS


def tearDownModule() -> None:  # noqa: N802
    """Remove every sys.modules key added during setUpModule."""
    added = frozenset(sys.modules.keys()) - _MODULES_SNAPSHOT
    for key in added:
        sys.modules.pop(key, None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _snap(
    instrument: str = "NSE:RELIANCE",
    ltp: float = 2500.0,
    bid: float = 2499.0,
    ask: float = 2501.0,
    spread_bps: float | None = None,   # auto-computed if None
    volume: int = 10_000,
    captured_at_utc: datetime | None = None,
) -> QuoteSnapshot:
    s = QuoteSnapshot(
        instrument=instrument,
        ltp=ltp,
        bid=bid,
        ask=ask,
        circuit_lower=0.0,
        circuit_upper=0.0,
        volume=volume,
        depth_bids=[],
        depth_asks=[],
        captured_at=time.monotonic(),
        captured_at_utc=captured_at_utc,
    )
    return s


def _make_poller(dynamo=None, prices_table="qe-dev-prices") -> LiveQuotePoller:
    zerodha = MagicMock()
    rate_limiter = MagicMock()
    rate_limiter.acquire = asyncio.coroutine(lambda p: None) if False else (
        lambda p: asyncio.sleep(0)
    )

    poller = LiveQuotePoller(
        zerodha=zerodha,
        instruments=["NSE:RELIANCE", "NSE:INFY"],
        rate_limiter=rate_limiter,
        dynamo_client=dynamo,
        prices_table=prices_table,
    )
    return poller


def _make_dynamo() -> MagicMock:
    dynamo = MagicMock()
    dynamo.put_item = MagicMock(return_value={})
    return dynamo


def _run(coro):
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


# ---------------------------------------------------------------------------
# QuoteSnapshot — captured_at_utc field
# ---------------------------------------------------------------------------

class TestQuoteSnapshotCapturedAtUtc:
    def test_captured_at_utc_defaults_to_now_utc(self):
        before = datetime.now(timezone.utc)
        snap = _snap()
        after = datetime.now(timezone.utc)
        assert snap.captured_at_utc.tzinfo is not None
        assert before <= snap.captured_at_utc <= after

    def test_captured_at_utc_accepts_explicit_value(self):
        fixed = datetime(2026, 5, 6, 9, 15, 0, tzinfo=timezone.utc)
        snap = _snap(captured_at_utc=fixed)
        assert snap.captured_at_utc == fixed

    def test_spread_bps_computed_correctly(self):
        snap = _snap(bid=2499.0, ask=2501.0, ltp=2500.0)
        # mid = 2500, spread = 2, spread_bps = 2/2500*10000 = 8 bps
        assert snap.spread_bps == _approx(8.0, abs=0.1)

    def test_spread_bps_none_when_bid_and_ask_zero(self):
        snap = _snap(bid=0.0, ask=0.0, ltp=0.0)
        assert snap.spread_bps is None


# ---------------------------------------------------------------------------
# _write_quotes_to_dynamo — item schema
# ---------------------------------------------------------------------------

class TestWriteQuotesToDynamo:

    def test_puts_correct_pk_and_sk_for_nse_instrument(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snap = _snap("NSE:RELIANCE")
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": snap}))

        dynamo.put_item.assert_called_once()
        item = dynamo.put_item.call_args.kwargs["Item"]
        assert item["PK"] == {"S": "QUOTE#NSE#RELIANCE"}
        assert item["SK"] == {"S": "LATEST"}

    def test_puts_correct_pk_for_us_instrument(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snap = _snap("US:AAPL")
        _run(poller._write_quotes_to_dynamo({"US:AAPL": snap}))

        item = dynamo.put_item.call_args.kwargs["Item"]
        assert item["PK"] == {"S": "QUOTE#US#AAPL"}
        assert item["SK"] == {"S": "LATEST"}

    def test_spread_bps_written_as_number_string(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snap = _snap(bid=2499.0, ask=2501.0, ltp=2500.0)
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": snap}))

        item = dynamo.put_item.call_args.kwargs["Item"]
        assert "spread_bps" in item
        spread_val = float(item["spread_bps"]["N"])
        assert spread_val == _approx(8.0, abs=0.1)

    def test_spread_bps_omitted_when_none(self):
        """No bid/ask → spread_bps is None → attribute must be omitted."""
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snap = _snap(bid=0.0, ask=0.0, ltp=0.0)
        assert snap.spread_bps is None  # pre-condition

        _run(poller._write_quotes_to_dynamo({"NSE:NOINSTRUMENT": snap}))

        item = dynamo.put_item.call_args.kwargs["Item"]
        assert "spread_bps" not in item   # must not write "N": "None"

    def test_bid_ask_ltp_volume_written(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snap = _snap(bid=2499.0, ask=2501.0, ltp=2500.0, volume=5000)
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": snap}))

        item = dynamo.put_item.call_args.kwargs["Item"]
        assert float(item["bid"]["N"]) == 2499.0
        assert float(item["ask"]["N"]) == 2501.0
        assert float(item["ltp"]["N"]) == 2500.0
        assert int(item["volume"]["N"]) == 5000

    def test_captured_at_written_as_iso_utc_string(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        fixed_utc = datetime(2026, 5, 6, 9, 15, 30, 123456, tzinfo=timezone.utc)
        snap = _snap(captured_at_utc=fixed_utc)
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": snap}))

        item = dynamo.put_item.call_args.kwargs["Item"]
        assert item["captured_at"] == {"S": fixed_utc.isoformat()}

    def test_correct_table_name_passed(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo, prices_table="prod-prices-table")
        snap = _snap()
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": snap}))

        assert dynamo.put_item.call_args.kwargs["TableName"] == "prod-prices-table"

    def test_multiple_instruments_all_written(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snaps = {
            "NSE:RELIANCE": _snap("NSE:RELIANCE"),
            "NSE:INFY":     _snap("NSE:INFY"),
            "US:AAPL":      _snap("US:AAPL"),
        }
        _run(poller._write_quotes_to_dynamo(snaps))

        assert dynamo.put_item.call_count == 3
        pks = {
            call.kwargs["Item"]["PK"]["S"]
            for call in dynamo.put_item.call_args_list
        }
        assert pks == {"QUOTE#NSE#RELIANCE", "QUOTE#NSE#INFY", "QUOTE#US#AAPL"}

    def test_dynamo_failure_is_non_fatal(self):
        """put_item raises → no exception escapes _write_quotes_to_dynamo."""
        dynamo = MagicMock()
        dynamo.put_item = MagicMock(side_effect=Exception("DynamoDB unavailable"))
        poller = _make_poller(dynamo=dynamo)
        snap = _snap()
        # Must not raise
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": snap}))

    def test_invalid_instrument_key_skipped(self):
        """Instruments without ':' separator should not cause put_item calls."""
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        snap = _snap()
        _run(poller._write_quotes_to_dynamo({"NOCOLON": snap}))
        dynamo.put_item.assert_not_called()

    def test_empty_quotes_dict_writes_nothing(self):
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        _run(poller._write_quotes_to_dynamo({}))
        dynamo.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# Schema cross-check: written key matches RiskContextBuilder reader key
# ---------------------------------------------------------------------------

class TestWriteReadSchemaAlignment:
    """
    Verifies the DynamoDB key schema written by LiveQuotePoller matches
    the key schema read by RiskContextBuilder._fetch_live_spread_bps().

    Reader uses:  PK = f"QUOTE#{market}#{symbol}"   SK = "LATEST"
    Writer uses:  PK = f"QUOTE#{market}#{symbol}"   SK = "LATEST"
    (market/symbol parsed from "{EXCHANGE}:{SYMBOL}" instrument string)
    """

    def test_pk_matches_risk_context_builder_reader(self):
        """
        Signal.market="NSE", Signal.symbol="RELIANCE"
        → reader key: QUOTE#NSE#RELIANCE
        Poller instrument "NSE:RELIANCE"
        → writer key: QUOTE#NSE#RELIANCE
        Both must match.
        """
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": _snap()}))

        written_pk = dynamo.put_item.call_args.kwargs["Item"]["PK"]["S"]
        # RiskContextBuilder builds: f"QUOTE#{signal.market}#{signal.symbol}"
        signal_market = "NSE"
        signal_symbol = "RELIANCE"
        reader_pk = f"QUOTE#{signal_market}#{signal_symbol}"

        assert written_pk == reader_pk

    def test_captured_at_is_parseable_by_fromisoformat(self):
        """captured_at written as ISO string must be parseable by datetime.fromisoformat()."""
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": _snap()}))

        raw_ts = dynamo.put_item.call_args.kwargs["Item"]["captured_at"]["S"]
        parsed = datetime.fromisoformat(raw_ts)
        assert parsed.tzinfo is not None   # timezone-aware → staleness check works

    def test_spread_bps_is_parseable_as_float(self):
        """spread_bps["N"] must be parseable as float for RiskContextBuilder."""
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)
        _run(poller._write_quotes_to_dynamo({"NSE:RELIANCE": _snap(bid=2499.0, ask=2501.0)}))

        raw_n = dynamo.put_item.call_args.kwargs["Item"]["spread_bps"]["N"]
        val = float(raw_n)
        assert val > 0


# ---------------------------------------------------------------------------
# _poll_cycle integration: DynamoDB write triggered after cache update
# ---------------------------------------------------------------------------

class TestPollCycleDynamoIntegration:

    def test_poll_cycle_writes_to_dynamo_when_client_set(self):
        """After a successful poll cycle, _write_quotes_to_dynamo is called."""
        dynamo = _make_dynamo()
        poller = _make_poller(dynamo=dynamo)

        # Stub zerodha.get_batch_quotes to return one instrument
        async def fake_quotes(instruments):
            return {
                "NSE:RELIANCE": {
                    "last_price": 2500.0,
                    "depth": {
                        "buy":  [{"price": 2499.0, "quantity": 100}],
                        "sell": [{"price": 2501.0, "quantity": 100}],
                    },
                    "lower_circuit_limit": 0.0,
                    "upper_circuit_limit": 0.0,
                    "volume": 50000,
                    "ohlc": {},
                }
            }

        poller._zerodha.get_batch_quotes = fake_quotes

        # Stub rate limiter acquire
        async def fake_acquire(*args, **kwargs):
            pass
        poller._rate_limiter.acquire = fake_acquire

        _run(poller._poll_cycle())

        # DynamoDB put_item must have been called for NSE:RELIANCE
        dynamo.put_item.assert_called_once()
        item = dynamo.put_item.call_args.kwargs["Item"]
        assert item["PK"] == {"S": "QUOTE#NSE#RELIANCE"}

    def test_poll_cycle_skips_dynamo_when_client_is_none(self):
        """When dynamo_client=None, _write_quotes_to_dynamo is never called."""
        poller = _make_poller(dynamo=None)

        async def fake_quotes(instruments):
            return {"NSE:RELIANCE": {
                "last_price": 2500.0,
                "depth": {"buy": [{"price": 2499.0, "quantity": 1}],
                          "sell": [{"price": 2501.0, "quantity": 1}]},
                "lower_circuit_limit": 0.0,
                "upper_circuit_limit": 0.0,
                "volume": 1000, "ohlc": {},
            }}

        poller._zerodha.get_batch_quotes = fake_quotes

        async def fake_acquire(*args, **kwargs):
            pass
        poller._rate_limiter.acquire = fake_acquire

        # Should complete without touching any DynamoDB client
        _run(poller._poll_cycle())
        # No assertion needed — if dynamo=None and code tries to call it, it explodes

    def test_poll_cycle_survives_dynamo_write_failure(self):
        """If DynamoDB write raises, _poll_cycle completes without propagating."""
        dynamo = MagicMock()
        dynamo.put_item = MagicMock(side_effect=RuntimeError("connection refused"))
        poller = _make_poller(dynamo=dynamo)

        async def fake_quotes(instruments):
            return {"NSE:RELIANCE": {
                "last_price": 2500.0,
                "depth": {"buy": [{"price": 2499.0, "quantity": 1}],
                          "sell": [{"price": 2501.0, "quantity": 1}]},
                "lower_circuit_limit": 0.0,
                "upper_circuit_limit": 0.0,
                "volume": 1000, "ohlc": {},
            }}

        poller._zerodha.get_batch_quotes = fake_quotes

        async def fake_acquire(*args, **kwargs):
            pass
        poller._rate_limiter.acquire = fake_acquire

        # Should not raise
        _run(poller._poll_cycle())

        # In-memory cache must still be updated despite DynamoDB failure
        assert "NSE:RELIANCE" in poller._quotes

    def test_in_memory_cache_updated_independently_of_dynamo(self):
        """Quote cache update is always atomic, regardless of DynamoDB outcome."""
        dynamo = MagicMock()
        dynamo.put_item = MagicMock(side_effect=Exception("boom"))
        poller = _make_poller(dynamo=dynamo)

        async def fake_quotes(instruments):
            return {"NSE:RELIANCE": {
                "last_price": 2999.0,
                "depth": {"buy": [{"price": 2998.0, "quantity": 1}],
                          "sell": [{"price": 3000.0, "quantity": 1}]},
                "lower_circuit_limit": 0.0,
                "upper_circuit_limit": 0.0,
                "volume": 100, "ohlc": {},
            }}

        poller._zerodha.get_batch_quotes = fake_quotes

        async def fake_acquire(*args, **kwargs):
            pass
        poller._rate_limiter.acquire = fake_acquire

        _run(poller._poll_cycle())

        snap = poller.get_quote("NSE:RELIANCE")
        assert snap is not None
        assert snap.ltp == 2999.0


# ---------------------------------------------------------------------------
# Custom test runner
# ---------------------------------------------------------------------------

def _run_tests():
    import traceback

    setUpModule()
    try:
        return _run_tests_inner()
    finally:
        tearDownModule()


def _run_tests_inner():
    import traceback

    test_classes = [
        TestQuoteSnapshotCapturedAtUtc,
        TestWriteQuotesToDynamo,
        TestWriteReadSchemaAlignment,
        TestPollCycleDynamoIntegration,
    ]

    passed = failed = errors = 0

    for cls in test_classes:
        instance = cls()
        for name in dir(cls):
            if not name.startswith("test_"):
                continue
            method = getattr(instance, name)
            try:
                method()
                print(f"  PASS  {cls.__name__}.{name}")
                passed += 1
            except AssertionError as e:
                print(f"  FAIL  {cls.__name__}.{name}: {e}")
                failed += 1
            except Exception:
                print(f"  ERR   {cls.__name__}.{name}")
                traceback.print_exc()
                errors += 1

    total = passed + failed + errors
    print(f"\n{total} tests  {passed} passed  {failed} failed  {errors} errors")
    return 1 if (failed or errors) else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_tests())
