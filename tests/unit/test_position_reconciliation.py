"""
Phase 3 unit tests — PositionReconciliationService.

Covers:
    - Detection of open position without exit policy (UNMANAGED)
    - Detection of open position with quantity=0 (ZERO_QTY_OPEN)
    - Detection of direction/quantity mismatch (QTY_DIRECTION)
    - Detection of stale exit_order_id on open position (STALE_EXIT_LOCK)
    - Paper mode: ZERO_QTY_OPEN is repaired (direction set to FLAT)
    - Live mode: no auto-repair, CRITICAL alert emitted
    - Reconciliation does not create duplicate exit orders
    - Clean scan returns empty mismatch list
    - Invalid mode raises ValueError
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── import path setup ────────────────────────────────────────────────────────


def _setup_paths() -> None:
    project_root = Path(__file__).resolve().parents[2]
    services_dir = project_root / "services"
    for p in (project_root, services_dir):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)

    if "structlog" not in sys.modules:
        sl = types.ModuleType("structlog")
        sl.get_logger = lambda *a, **kw: MagicMock()  # type: ignore[attr-defined]
        sys.modules["structlog"] = sl

    import services.execution_engine as _ee
    import services.shared as _sh
    import services.shared.logging as _shl
    import services.shared.logging.logger as _shll

    sys.modules.setdefault("shared", _sh)
    sys.modules.setdefault("shared.logging", _shl)
    sys.modules.setdefault("shared.logging.logger", _shll)
    sys.modules.setdefault("execution_engine", _ee)


_setup_paths()

from services.execution_engine.reconciliation.reconciliation import (  # noqa: E402
    MismatchType,
    PositionReconciliationService,
    ReconciliationReport,
)


# ── helpers ───────────────────────────────────────────────────────────────────


def _dynamo_item(
    symbol: str,
    *,
    direction: str,
    quantity: float,
    stop_price: float | None = None,
    exit_order_id: str | None = None,
    exit_state: str | None = None,
) -> dict:
    item: dict = {
        "symbol":    {"S": symbol},
        "direction": {"S": direction},
        "quantity":  {"N": str(quantity)},
    }
    if stop_price is not None:
        item["stop_price"] = {"N": str(stop_price)}
    if exit_order_id is not None:
        item["exit_order_id"] = {"S": exit_order_id}
    if exit_state is not None:
        item["exit_state"] = {"S": exit_state}
    return item


def _make_dynamo(items: list[dict]) -> MagicMock:
    dynamo = MagicMock()
    dynamo.scan.return_value = {"Items": items}
    dynamo.update_item.return_value = {}
    return dynamo


def _make_service(items: list[dict], *, mode: str = "paper") -> PositionReconciliationService:
    return PositionReconciliationService(
        dynamo_client=_make_dynamo(items),
        positions_table="test-positions",
        mode=mode,
    )


# ── TestMismatchDetection ────────────────────────────────────────────────────


class TestMismatchDetection:
    """Unit tests for _detect_mismatches() — no DynamoDB I/O."""

    def _svc(self) -> PositionReconciliationService:
        return PositionReconciliationService(
            dynamo_client=MagicMock(),
            positions_table="t",
            mode="paper",
        )

    def test_clean_position_has_no_mismatches(self) -> None:
        item = _dynamo_item("MARUTI", direction="LONG", quantity=7.0, stop_price=1300.0)
        mismatches = self._svc()._detect_mismatches(item)
        assert mismatches == []

    def test_open_position_without_stop_is_unmanaged(self) -> None:
        item = _dynamo_item("MARUTI", direction="LONG", quantity=7.0)
        mismatches = self._svc()._detect_mismatches(item)
        types_ = {m.mismatch_type for m in mismatches}
        assert MismatchType.UNMANAGED in types_

    def test_open_position_zero_quantity_is_zero_qty_open(self) -> None:
        item = _dynamo_item("MARUTI", direction="LONG", quantity=0.0, stop_price=1300.0)
        mismatches = self._svc()._detect_mismatches(item)
        types_ = {m.mismatch_type for m in mismatches}
        assert MismatchType.ZERO_QTY_OPEN in types_

    def test_qty_direction_mismatch_detected(self) -> None:
        # direction says LONG but signed quantity is negative → SHORT
        item = _dynamo_item("ICICIBANK", direction="LONG", quantity=-78.0, stop_price=950.0)
        mismatches = self._svc()._detect_mismatches(item)
        types_ = {m.mismatch_type for m in mismatches}
        assert MismatchType.QTY_DIRECTION in types_

    def test_stale_exit_lock_detected(self) -> None:
        # exit_order_id set but position still LONG (not yet FLAT)
        item = _dynamo_item(
            "NHPC", direction="LONG", quantity=1270.0, stop_price=72.0,
            exit_order_id="EXIT-NHPC-NSE-STOP_LOSS-2026-05-25",
        )
        mismatches = self._svc()._detect_mismatches(item)
        types_ = {m.mismatch_type for m in mismatches}
        assert MismatchType.STALE_EXIT_LOCK in types_

    def test_flat_position_with_stop_is_flat_with_stop(self) -> None:
        item = _dynamo_item("ATGL", direction="FLAT", quantity=0.0, stop_price=580.0)
        mismatches = self._svc()._detect_mismatches(item)
        types_ = {m.mismatch_type for m in mismatches}
        assert MismatchType.FLAT_WITH_STOP in types_

    def test_flat_position_without_stop_is_clean(self) -> None:
        item = _dynamo_item("ATGL", direction="FLAT", quantity=0.0)
        mismatches = self._svc()._detect_mismatches(item)
        assert mismatches == []

    def test_short_position_with_negative_qty_and_stop_is_clean(self) -> None:
        item = _dynamo_item(
            "ICICIBANK", direction="SHORT", quantity=-78.0, stop_price=950.0
        )
        mismatches = self._svc()._detect_mismatches(item)
        assert mismatches == []


# ── TestPaperModeRun ─────────────────────────────────────────────────────────


class TestPaperModeRun:
    """Full reconciliation run in paper mode."""

    async def test_clean_positions_produce_clean_report(self) -> None:
        items = [
            _dynamo_item("MARUTI",    direction="LONG",  quantity=7.0,    stop_price=1300.0),
            _dynamo_item("ICICIBANK", direction="SHORT", quantity=-78.0,   stop_price=950.0),
            _dynamo_item("NHPC",      direction="FLAT",  quantity=0.0),
        ]
        svc = _make_service(items)
        report = await svc.run()
        assert report.clean is True
        assert report.total_mismatches == 0

    async def test_unmanaged_position_detected_in_paper_mode(self) -> None:
        items = [
            _dynamo_item("MARUTI", direction="LONG", quantity=7.0),  # no stop_price
        ]
        svc = _make_service(items)
        report = await svc.run()
        types_ = {m.mismatch_type for m in report.mismatches}
        assert MismatchType.UNMANAGED in types_

    async def test_zero_qty_open_is_repaired_in_paper_mode(self) -> None:
        items = [
            _dynamo_item("MARUTI", direction="LONG", quantity=0.0, stop_price=1300.0),
        ]
        dynamo = _make_dynamo(items)
        svc = PositionReconciliationService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            mode="paper",
        )
        report = await svc.run()

        # The mismatch was detected
        types_ = {m.mismatch_type for m in report.mismatches}
        assert MismatchType.ZERO_QTY_OPEN in types_

        # And it was repaired (update_item was called to set direction=FLAT)
        assert report.repaired == 1
        dynamo.update_item.assert_called_once()
        call_kwargs = dynamo.update_item.call_args[1]
        assert "direction = :flat" in call_kwargs.get("UpdateExpression", "")

    async def test_multiple_mismatches_all_detected(self) -> None:
        items = [
            _dynamo_item("MARUTI", direction="LONG", quantity=7.0),          # UNMANAGED
            _dynamo_item("NHPC",   direction="LONG", quantity=0.0, stop_price=72.0),  # ZERO_QTY_OPEN
            _dynamo_item("CLEAN",  direction="SHORT", quantity=-78.0, stop_price=950.0),
        ]
        svc = _make_service(items)
        report = await svc.run()

        types_ = {m.mismatch_type for m in report.mismatches}
        assert MismatchType.UNMANAGED in types_
        assert MismatchType.ZERO_QTY_OPEN in types_
        # CLEAN has no mismatches
        mismatch_symbols = {m.symbol for m in report.mismatches}
        assert "CLEAN" not in mismatch_symbols


# ── TestLiveModeRun ───────────────────────────────────────────────────────────


class TestLiveModeRun:
    """Live mode: no auto-repair, CRITICAL log emitted."""

    async def test_live_mode_does_not_repair_zero_qty_open(self) -> None:
        items = [
            _dynamo_item("MARUTI", direction="LONG", quantity=0.0, stop_price=1300.0),
        ]
        dynamo = _make_dynamo(items)
        svc = PositionReconciliationService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            mode="live",
        )
        report = await svc.run()

        assert MismatchType.ZERO_QTY_OPEN in {m.mismatch_type for m in report.mismatches}
        assert report.repaired == 0, "live mode must not repair anything"
        assert report.alerted >= 1
        dynamo.update_item.assert_not_called()

    async def test_live_mode_emits_critical_for_unmanaged(self) -> None:
        items = [
            _dynamo_item("NHPC", direction="LONG", quantity=1270.0),  # no stop_price
        ]
        dynamo = _make_dynamo(items)
        svc = PositionReconciliationService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            mode="live",
        )
        logger_mock = MagicMock()

        with patch(
            "services.execution_engine.reconciliation.reconciliation.logger",
            logger_mock,
        ):
            report = await svc.run()

        assert report.repaired == 0
        # critical was called at least once (for the unmanaged position)
        logger_mock.critical.assert_called()


# ── TestIdempotency ───────────────────────────────────────────────────────────


class TestReconciliationIdempotency:
    """Reconciliation must not create duplicate exit orders."""

    async def test_reconciliation_does_not_call_exit_router(self) -> None:
        """
        PositionReconciliationService has no ExitOrderRouter dependency.
        It must not place any exit orders — it only reads and repairs metadata.
        """
        import inspect
        import services.execution_engine.reconciliation.reconciliation as m

        source = inspect.getsource(m)
        assert "ExitOrderRouter" not in source
        assert "router.route" not in source
        assert "exit_order_router" not in source

    async def test_stale_exit_lock_not_cleared_automatically(self) -> None:
        """
        STALE_EXIT_LOCK is flagged as WARNING but NOT auto-repaired.
        Clearing exit_order_id could allow duplicate exits.
        """
        items = [
            _dynamo_item(
                "MARUTI", direction="LONG", quantity=7.0, stop_price=1300.0,
                exit_order_id="EXIT-MARUTI-NSE-STOP_LOSS-2026-05-25",
            ),
        ]
        dynamo = _make_dynamo(items)
        svc = PositionReconciliationService(
            dynamo_client=dynamo,
            positions_table="test-positions",
            mode="paper",
        )
        report = await svc.run()

        assert MismatchType.STALE_EXIT_LOCK in {m.mismatch_type for m in report.mismatches}
        # exit_order_id must NOT be cleared — update_item must not be called
        dynamo.update_item.assert_not_called()


# ── TestInvalidMode ───────────────────────────────────────────────────────────


class TestInvalidMode:
    def test_invalid_mode_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="paper.*live"):
            PositionReconciliationService(
                dynamo_client=MagicMock(),
                positions_table="t",
                mode="backtest",  # invalid
            )
