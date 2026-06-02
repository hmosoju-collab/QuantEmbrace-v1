"""
Phase 3.1 unit tests — ExecutionService startup position reconciliation.

Tests _run_startup_position_reconciliation() in isolation.  The method is
called directly (as an unbound function) with a minimal fake-service object,
so the test does NOT need Kafka, brokers, DynamoDB tables, or any live
infrastructure.

Covers:
  - paper mode: PositionReconciliationService called before TEE starts
  - paper mode: safe mismatches repaired; report stored on service
  - live mode: mode='live' passed; no repair; report stored
  - backtest mode: reconciliation skipped entirely
  - reconciliation_enabled=False: skipped
  - reconciliation_run_on_startup=False: skipped
  - reconciliation failure + strict_startup=False: ERROR log, no crash
  - reconciliation failure + strict_startup=True: RuntimeError raised
  - strict_startup=True + unresolvable mismatches: RuntimeError raised
  - unmanaged position: UNMANAGED in report; no crash (paper, non-strict)
  - _reconciliation_report attribute set after run
  - ordering invariant (source inspection): reconciliation before TEE task
"""
from __future__ import annotations

import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


# ── Import path bootstrap ─────────────────────────────────────────────────────


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

    # Alias all execution_engine sub-packages BEFORE service.py is imported.
    #
    # service.py has module-level imports such as:
    #   from execution_engine.monitors.orphan_detector import OrphanDetector
    # These side-effect sys.modules by adding entries under the "execution_engine.*"
    # keys.  Because execution_engine == services.execution_engine (same object),
    # the .monitors attribute on the shared module object gets set to whichever
    # module was registered first.  If test_trade_exit_engine.py later tries to
    # import services.execution_engine.monitors.trade_exit_engine, Python finds
    # the (partially populated) execution_engine.monitors instead of doing a fresh
    # load, causing "cannot import name trade_exit_engine".
    #
    # Pre-registering under both keys ensures both paths resolve to the same
    # module object so no "first-come-first-cached" confusion occurs.
    import services.execution_engine.monitors as _mon_pkg
    import services.execution_engine.monitors.orphan_detector as _orphan
    import services.execution_engine.monitors.trade_exit_engine as _tee
    import services.execution_engine.reconciliation as _rec_pkg
    import services.execution_engine.reconciliation.reconciliation as _rec_mod

    sys.modules.setdefault("execution_engine.monitors", _mon_pkg)
    sys.modules.setdefault("execution_engine.monitors.orphan_detector", _orphan)
    sys.modules.setdefault("execution_engine.monitors.trade_exit_engine", _tee)
    sys.modules.setdefault("execution_engine.reconciliation", _rec_pkg)
    sys.modules.setdefault(
        "execution_engine.reconciliation.reconciliation", _rec_mod
    )


_setup_paths()

import services.execution_engine.service as _svc_module  # noqa: E402
from services.execution_engine.reconciliation.reconciliation import (  # noqa: E402
    MismatchType,
    PositionMismatch,
    PositionReconciliationService,
    ReconciliationReport,
)


# ── Minimal fake service ──────────────────────────────────────────────────────


def _make_settings(
    *,
    paper_trading: bool = True,
    backtest_mode: bool = False,
    reconciliation_enabled: bool = True,
    reconciliation_run_on_startup: bool = True,
    reconciliation_strict_startup: bool = False,
) -> SimpleNamespace:
    execution = SimpleNamespace(
        paper_trading=paper_trading,
        backtest_mode=backtest_mode,
        reconciliation_enabled=reconciliation_enabled,
        reconciliation_run_on_startup=reconciliation_run_on_startup,
        reconciliation_strict_startup=reconciliation_strict_startup,
    )
    aws = SimpleNamespace(dynamodb_table_positions="test-positions")
    return SimpleNamespace(execution=execution, aws=aws)


def _make_svc(settings: SimpleNamespace, dynamo: object = None) -> SimpleNamespace:
    """Minimal stand-in for ExecutionService with just the attrs the method reads."""
    return SimpleNamespace(
        _settings=settings,
        _dynamo=dynamo or MagicMock(),
        _reconciliation_report=None,
    )


async def _call(svc: SimpleNamespace) -> None:
    """Call the unbound method explicitly so 'self' is the fake service."""
    await _svc_module.ExecutionService._run_startup_position_reconciliation(svc)


# ── Report factories ──────────────────────────────────────────────────────────


def _clean(mode: str = "paper") -> ReconciliationReport:
    return ReconciliationReport(mode=mode)


def _repaired(mode: str = "paper") -> ReconciliationReport:
    r = ReconciliationReport(mode=mode)
    m = PositionMismatch(
        symbol="MARUTI",
        mismatch_type=MismatchType.ZERO_QTY_OPEN,
        detail="qty=0 but direction=LONG",
        repaired=True,
    )
    r.mismatches.append(m)
    r.repaired = 1
    return r


def _unmanaged(mode: str = "paper") -> ReconciliationReport:
    r = ReconciliationReport(mode=mode)
    m = PositionMismatch(
        symbol="NHPC",
        mismatch_type=MismatchType.UNMANAGED,
        detail="no stop_price",
    )
    r.mismatches.append(m)
    r.alerted = 1
    return r


def _live_critical() -> ReconciliationReport:
    r = ReconciliationReport(mode="live")
    m = PositionMismatch(
        symbol="NHPC",
        mismatch_type=MismatchType.ZERO_QTY_OPEN,
        detail="qty=0 but direction=LONG",
    )
    r.mismatches.append(m)
    r.alerted = 1
    return r


# ── TestSkipConditions ────────────────────────────────────────────────────────


class TestSkipConditions:
    """Reconciliation must be skipped; reconciler.run() must never be called."""

    async def _assert_run_not_called(self, settings: SimpleNamespace) -> None:
        svc = _make_svc(settings)
        with pytest.MonkeyPatch().context() as mp:
            run_mock = AsyncMock(return_value=_clean())
            mp.setattr(PositionReconciliationService, "run", run_mock)
            await _call(svc)
        run_mock.assert_not_called()

    async def test_skipped_when_reconciliation_disabled(self) -> None:
        await self._assert_run_not_called(
            _make_settings(reconciliation_enabled=False)
        )

    async def test_skipped_when_run_on_startup_false(self) -> None:
        await self._assert_run_not_called(
            _make_settings(reconciliation_run_on_startup=False)
        )

    async def test_skipped_in_backtest_mode(self) -> None:
        await self._assert_run_not_called(_make_settings(backtest_mode=True))

    async def test_report_stays_none_when_skipped(self) -> None:
        svc = _make_svc(_make_settings(reconciliation_enabled=False))
        await _call(svc)
        assert svc._reconciliation_report is None


# ── TestPaperModeRun ──────────────────────────────────────────────────────────


class TestPaperModeRun:
    """Paper mode: reconciler is called, report is stored, repairs reflected."""

    async def test_reconciler_called_with_mode_paper(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=True))
        captured: list[PositionReconciliationService] = []
        original_init = PositionReconciliationService.__init__

        def recording_init(self_r, dynamo_client, positions_table, mode):  # noqa: N802
            captured.append(mode)
            original_init(self_r, dynamo_client, positions_table, mode)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "__init__", recording_init)
            mp.setattr(
                PositionReconciliationService,
                "run",
                AsyncMock(return_value=_clean("paper")),
            )
            await _call(svc)

        assert captured == ["paper"]

    async def test_report_stored_after_run(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=True))
        report = _clean("paper")
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            await _call(svc)
        assert svc._reconciliation_report is report

    async def test_clean_report_does_not_raise(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=True))
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                PositionReconciliationService,
                "run",
                AsyncMock(return_value=_clean("paper")),
            )
            await _call(svc)  # must not raise

    async def test_repaired_mismatch_reflected_in_report(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=True))
        report = _repaired("paper")
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            await _call(svc)
        assert svc._reconciliation_report.repaired == 1
        assert svc._reconciliation_report.total_mismatches == 1

    async def test_unmanaged_position_does_not_crash_paper_non_strict(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=True))
        report = _unmanaged("paper")
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            await _call(svc)  # must not raise

        assert MismatchType.UNMANAGED in {
            m.mismatch_type for m in svc._reconciliation_report.mismatches
        }

    async def test_positions_table_passed_to_reconciler(self) -> None:
        settings = _make_settings(paper_trading=True)
        settings.aws.dynamodb_table_positions = "prod-positions-table"
        svc = _make_svc(settings)
        captured_table: list[str] = []
        original_init = PositionReconciliationService.__init__

        def recording_init(self_r, dynamo_client, positions_table, mode):  # noqa: N802
            captured_table.append(positions_table)
            original_init(self_r, dynamo_client, positions_table, mode)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "__init__", recording_init)
            mp.setattr(
                PositionReconciliationService,
                "run",
                AsyncMock(return_value=_clean("paper")),
            )
            await _call(svc)

        assert captured_table == ["prod-positions-table"]


# ── TestLiveModeRun ───────────────────────────────────────────────────────────


class TestLiveModeRun:
    """Live mode: reconciler called with mode='live'; no repair; report stored."""

    async def test_live_mode_passes_mode_live(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=False))
        captured: list[str] = []
        original_init = PositionReconciliationService.__init__

        def recording_init(self_r, dynamo_client, positions_table, mode):  # noqa: N802
            captured.append(mode)
            original_init(self_r, dynamo_client, positions_table, mode)

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "__init__", recording_init)
            mp.setattr(
                PositionReconciliationService,
                "run",
                AsyncMock(return_value=_clean("live")),
            )
            await _call(svc)

        assert captured == ["live"]

    async def test_live_mode_report_stored(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=False))
        report = _live_critical()
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            await _call(svc)
        assert svc._reconciliation_report is report

    async def test_live_mode_alerted_not_repaired(self) -> None:
        svc = _make_svc(_make_settings(paper_trading=False))
        report = _live_critical()
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            await _call(svc)
        assert svc._reconciliation_report.repaired == 0
        assert svc._reconciliation_report.alerted == 1


# ── TestStrictStartup ─────────────────────────────────────────────────────────


class TestStrictStartup:
    """strict_startup=True must raise RuntimeError on failure or unresolvable mismatches."""

    async def test_strict_false_clean_no_raise(self) -> None:
        svc = _make_svc(_make_settings(reconciliation_strict_startup=False))
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=_clean()))
            await _call(svc)

    async def test_strict_false_mismatches_no_raise(self) -> None:
        svc = _make_svc(_make_settings(reconciliation_strict_startup=False))
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                PositionReconciliationService, "run", AsyncMock(return_value=_unmanaged())
            )
            await _call(svc)

    async def test_strict_false_exception_does_not_crash(self) -> None:
        svc = _make_svc(_make_settings(reconciliation_strict_startup=False))
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                PositionReconciliationService,
                "run",
                AsyncMock(side_effect=Exception("DynamoDB unavailable")),
            )
            await _call(svc)  # must not raise
        assert svc._reconciliation_report is None

    async def test_strict_true_exception_raises(self) -> None:
        svc = _make_svc(_make_settings(reconciliation_strict_startup=True))
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(
                PositionReconciliationService,
                "run",
                AsyncMock(side_effect=Exception("DynamoDB unavailable")),
            )
            with pytest.raises(RuntimeError, match="reconciliation_strict_startup=True"):
                await _call(svc)

    async def test_strict_true_unresolvable_mismatches_raise(self) -> None:
        """mismatch with repaired=0 AND alerted=0 is unresolvable → RuntimeError."""
        svc = _make_svc(_make_settings(reconciliation_strict_startup=True))
        # Manufacture report where a mismatch is present but neither repaired nor alerted
        # (simulates a reconciler bug that silently drops a mismatch).
        report = ReconciliationReport(mode="paper")
        report.mismatches.append(
            PositionMismatch(
                symbol="MARUTI",
                mismatch_type=MismatchType.ZERO_QTY_OPEN,
                detail="unresolved",
            )
        )
        # repaired=0, alerted=0 → repaired+alerted (0) < total_mismatches (1) → unsafe

        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            with pytest.raises(RuntimeError, match="unresolvable"):
                await _call(svc)

    async def test_strict_true_alerted_mismatches_do_not_raise(self) -> None:
        """Mismatches that are fully alerted are 'safe' even in strict mode."""
        svc = _make_svc(_make_settings(reconciliation_strict_startup=True))
        report = _unmanaged("paper")  # alerted=1, total_mismatches=1 → safe
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", AsyncMock(return_value=report))
            await _call(svc)  # must not raise


# ── TestBacktestSkip ──────────────────────────────────────────────────────────


class TestBacktestSkip:
    """Backtest mode: reconciliation skipped, report stays None."""

    async def test_reconciler_run_never_called(self) -> None:
        svc = _make_svc(_make_settings(backtest_mode=True))
        run_mock = AsyncMock(return_value=_clean())
        with pytest.MonkeyPatch().context() as mp:
            mp.setattr(PositionReconciliationService, "run", run_mock)
            await _call(svc)
        run_mock.assert_not_called()

    async def test_report_stays_none_in_backtest(self) -> None:
        svc = _make_svc(_make_settings(backtest_mode=True))
        await _call(svc)
        assert svc._reconciliation_report is None


# ── TestOrderingInvariant ─────────────────────────────────────────────────────


class TestOrderingInvariant:
    """
    Source-level invariants: reconciliation appears before TEE launch in start().
    These are purely structural — no runtime needed.
    """

    def test_reconciliation_before_tee_in_start(self) -> None:
        import inspect

        source = inspect.getsource(_svc_module.ExecutionService.start)
        recon_idx = source.find("_run_startup_position_reconciliation")
        tee_idx = source.find("_trade_exit_engine.run")

        assert recon_idx != -1, "_run_startup_position_reconciliation not found in start()"
        assert tee_idx != -1, "_trade_exit_engine.run not found in start()"
        assert recon_idx < tee_idx, (
            "_run_startup_position_reconciliation must appear before "
            "_trade_exit_engine.run in start()"
        )

    def test_reconciliation_before_mis_manager_task(self) -> None:
        import inspect

        source = inspect.getsource(_svc_module.ExecutionService.start)
        recon_idx = source.find("_run_startup_position_reconciliation")
        mis_idx = source.find("mis_manager.run")

        assert recon_idx != -1
        assert mis_idx != -1
        assert recon_idx < mis_idx, (
            "Reconciliation must complete before MIS manager task is launched"
        )

    def test_reconciliation_method_never_calls_exit_router(self) -> None:
        import inspect

        source = inspect.getsource(
            _svc_module.ExecutionService._run_startup_position_reconciliation
        )
        assert "ExitOrderRouter" not in source
        assert "place_order" not in source

    def test_reconciliation_report_attribute_in_init(self) -> None:
        import inspect

        source = inspect.getsource(_svc_module.ExecutionService.__init__)
        assert "_reconciliation_report" in source

    def test_reconciliation_called_after_reconcile_state_in_start(self) -> None:
        import inspect

        source = inspect.getsource(_svc_module.ExecutionService.start)
        order_recon_idx = source.find("_reconcile_state")
        pos_recon_idx = source.find("_run_startup_position_reconciliation")

        assert order_recon_idx != -1, "_reconcile_state not found in start()"
        assert pos_recon_idx != -1
        assert order_recon_idx < pos_recon_idx, (
            "Order-level _reconcile_state must run before position-level "
            "_run_startup_position_reconciliation"
        )
