# Phase 4 Design Review — Observability, Paper Gates, Live Mode Gate

**Status:** AWAITING APPROVAL  
**Date:** 2026-05-25  
**Author:** Chief Architect  
**Scope:** Metrics emission, paper acceptance criteria, live gate documentation, and tests.  
**Live trading:** Remains blocked. `live_trading_enabled=False` is not changed in this phase.

---

## 1. Summary of Changes

Phase 4 adds exactly three categories of changes, nothing more:

1. **Metrics** — wire CloudWatch metric calls into TEE, MIS, reconciliation, and ExitOrderRouter (the only four components that currently emit zero metrics). No new infrastructure.
2. **Docs** — create paper acceptance checklist and live gate checklist (already written, see `/docs/operations/`).
3. **Tests** — unit tests for metric emission and a structural test that the live gate remains in place.

No new components. No new DynamoDB fields. No new Kafka topics. No broker calls added.

---

## 2. Metrics Design

### 2.1 Infrastructure

The `CloudWatchMetrics` client (`shared/metrics/cloudwatch_metrics.py`) is already implemented:
- `record_count(name, value, dimensions)` — fire-and-forget, never blocks
- `record_gauge(name, value, unit, dimensions)` — absolute value
- `flush()` — async, called via `asyncio.create_task` in hot paths

All metrics use namespace `"QuantEmbrace/Trading"` (same as `service.py`).  
DRY-RUN mode (no CloudWatch client) silently logs to DEBUG — safe for unit tests.

### 2.2 Metrics Injection Pattern

Each component receives the metrics client as a constructor parameter with a sensible default:

```python
from shared.metrics.cloudwatch_metrics import get_metrics_client, CloudWatchMetrics

class TradeExitEngine:
    def __init__(
        self,
        ...,
        metrics: Optional[CloudWatchMetrics] = None,
    ) -> None:
        self._metrics = metrics or get_metrics_client("QuantEmbrace/Trading")
```

This pattern:
- Allows unit tests to inject a `MagicMock()` and assert `record_count.assert_called_with(...)`
- Keeps the production path using the singleton (no extra boto3 client creation)
- Does not change any caller in `service.py` — the default argument handles it

### 2.3 Metric Definitions

#### TradeExitEngine metrics

| Metric name | Type | Dimensions | Emitted when |
|---|---|---|---|
| `TrailingStopActivated` | Counter | `Symbol`, `Direction`, `Mode` | `_activate_trailing_stop()` succeeds |
| `TrailingStopHit` | Counter | `Symbol`, `Direction`, `Mode` | `_fire_exit()` with `trigger=TRAILING` |
| `UnmanagedPositionDetected` | Counter | `Symbol`, `Mode` | `_alert_unmanaged_positions()` per symbol per cycle |
| `ExitFired` | Counter | `TriggerType`, `Direction`, `Mode` | `_fire_exit()` — every exit fired |

CW metric name convention: PascalCase (matches existing `OrderPlacementLatencyMs`, `OrdersSubmitted`).

**Implementation locations:**

```python
# _activate_trailing_stop() — after logger.info("tee.trailing_stop_activated", ...)
self._metrics.record_count(
    "TrailingStopActivated",
    dimensions={"Symbol": symbol, "Direction": direction, "Mode": self._mode},
)
asyncio.create_task(self._metrics.flush())

# _fire_exit() — after router.route(request)
self._metrics.record_count(
    "ExitFired",
    dimensions={"TriggerType": trigger.value, "Direction": direction, "Mode": self._mode},
)
if trigger == ExitTriggerType.TRAILING:
    self._metrics.record_count(
        "TrailingStopHit",
        dimensions={"Symbol": position["symbol"], "Direction": direction, "Mode": self._mode},
    )
asyncio.create_task(self._metrics.flush())

# _alert_unmanaged_positions() — inside the per-symbol loop
self._metrics.record_count(
    "UnmanagedPositionDetected",
    dimensions={"Symbol": symbol, "Mode": self._mode},
)
```

**`self._mode`** — add `mode: str = "paper"` to `TradeExitEngine.__init__`, set from settings in `service.py`. Currently TEE doesn't store mode directly (it delegates mode to the router). Add as a simple stored string.

#### PositionReconciliationService metrics

| Metric name | Type | Dimensions | Emitted when |
|---|---|---|---|
| `ReconciliationMismatchDetected` | Counter | `MismatchType`, `Mode` | `run()` per mismatch found |
| `ReconciliationPaperRepair` | Counter | `MismatchType` | `_repair()` when `mismatch.repaired = True` |
| `ReconciliationLiveAlert` | Counter | `MismatchType` | `_alert_critical()` called |

Emitted at the end of `run()` from the accumulated report:

```python
# In run(), after the positions loop, before the completion log:
for mismatch in report.mismatches:
    self._metrics.record_count(
        "ReconciliationMismatchDetected",
        dimensions={"MismatchType": mismatch.mismatch_type.value, "Mode": self._mode},
    )
    if mismatch.repaired:
        self._metrics.record_count(
            "ReconciliationPaperRepair",
            dimensions={"MismatchType": mismatch.mismatch_type.value},
        )
    elif self._mode == "live":
        self._metrics.record_count(
            "ReconciliationLiveAlert",
            dimensions={"MismatchType": mismatch.mismatch_type.value},
        )
await self._metrics.flush()
```

**Constructor change:**
```python
class PositionReconciliationService:
    def __init__(
        self,
        dynamo_client: Any,
        positions_table: str,
        mode: str = "paper",
        metrics: Optional[CloudWatchMetrics] = None,
    ) -> None:
        ...
        self._metrics = metrics or get_metrics_client("QuantEmbrace/Trading")
```

#### MISSquareOffManager metrics

| Metric name | Type | Dimensions | Emitted when |
|---|---|---|---|
| `MISPositionsFound` | Gauge | `Mode` | `_get_open_mis_positions()` — total count |
| `MISLongPositionsFound` | Gauge | `Mode` | Same — LONG count |
| `MISShortPositionsFound` | Gauge | `Mode` | Same — SHORT count |
| `MISOrderPlaced` | Counter | `Side`, `Mode` | `_place_mis_close_order()` success |
| `MISOrderRejected` | Counter | `Mode` | `_place_mis_close_order()` exception |
| `MISPositionConfirmedFlat` | Counter | `Mode` | Fill confirmed in `_await_fills_or_escalate` |
| `MISPositionsOpenAtDeadline` | Gauge | `Mode` | At 15:10 deadline — unclosed count |
| `MISKillSwitchActivated` | Counter | `Mode` | Deadline escalation → kill switch |
| `MISDuplicatesPrevented` | Counter | `Mode` | `ExitOrderRouter._acquire_exit_lock` returns False |
| `MISProductTypeMIS` | Counter | — | `_place_mis_close_order()` with `product_type=MIS` |
| `MISProductTypeInvalid` | Counter | `ActualType` | `_place_mis_close_order()` with wrong product_type |

**Key implementation notes:**

`MISDuplicatesPrevented` is emitted in `_place_mis_close_order()` when `ExitOrderRouter.route()` returns `False` (idempotency check caught it). This requires MIS to check the return value, which it currently ignores.

`MISProductTypeInvalid` should never be non-zero in a correctly running system. It is a sentinel for the `ProductType.MIS` bug regression (the Phase 1 bug that missed short positions due to wrong filter — a different regression, but same category of "silent wrong product_type"). Guard:

```python
if request.product_type != ProductType.MIS:
    logger.critical("mis_square_off.invalid_product_type", ...)
    self._metrics.record_count(
        "MISProductTypeInvalid",
        dimensions={"ActualType": str(request.product_type)},
    )
```

**Constructor change for MIS:**
```python
class MISSquareOffManager:
    def __init__(
        self,
        ...,
        metrics: Optional[CloudWatchMetrics] = None,
    ) -> None:
        ...
        self._metrics = metrics or get_metrics_client("QuantEmbrace/Trading")
```

### 2.4 Flushing Strategy

All metric calls use `asyncio.create_task(self._metrics.flush())` — the same fire-and-forget pattern already used in `service.py`. No blocking, no await in hot paths.

### 2.5 service.py wiring

The `service.py` constructor calls for MIS, TEE, and reconciliation remain unchanged. The `metrics=None` default means the singleton is used automatically. No changes to `service.py` needed for metrics.

Exception: TEE needs `mode` added:
```python
# service.py: TradeExitEngine construction
self._trade_exit_engine = TradeExitEngine(
    dynamo_client=dynamo,
    positions_table=self._settings.aws.dynamodb_table_positions,
    router=_exit_router,
    prices_table=self._settings.aws.dynamodb_table_prices,
    mode="paper" if is_paper else "live",   # NEW
)
```

---

## 3. Test Design

### 3.1 New test file: `tests/unit/test_phase4_metrics.py`

26 tests across 4 classes.

#### `TestTEEMetrics` (7 tests)

| Test | Validates |
|---|---|
| `test_trailing_activation_emits_metric` | `record_count("TrailingStopActivated", ...)` called when trailing activates |
| `test_trailing_hit_emits_metric` | `record_count("TrailingStopHit", ...)` called on TRAILING trigger in `_fire_exit` |
| `test_stop_loss_exit_emits_exit_fired` | `record_count("ExitFired", dimensions={"TriggerType": "STOP_LOSS"})` |
| `test_take_profit_exit_emits_exit_fired` | `record_count("ExitFired", dimensions={"TriggerType": "TAKE_PROFIT"})` |
| `test_unmanaged_emits_metric` | `record_count("UnmanagedPositionDetected", ...)` called for each unmanaged position |
| `test_clean_cycle_emits_nothing` | No `record_count` calls when no exit and no trailing activation |
| `test_metrics_flush_called_after_exit` | `flush()` called (as task) after each `_fire_exit` |

**Test pattern:**
```python
mock_metrics = MagicMock()
tee = TradeExitEngine(
    dynamo_client=make_dynamo([position]),
    positions_table="t",
    router=make_router(),
    metrics=mock_metrics,
)
await tee._activate_trailing_stop(position, last_price=105.0)
mock_metrics.record_count.assert_called_once_with(
    "TrailingStopActivated",
    dimensions={"Symbol": "MARUTI", "Direction": "LONG", "Mode": "paper"},
)
```

#### `TestReconciliationMetrics` (6 tests)

| Test | Validates |
|---|---|
| `test_clean_run_emits_zero_mismatch_metrics` | No `ReconciliationMismatchDetected` calls on clean scan |
| `test_mismatch_emits_metric_per_type` | One `ReconciliationMismatchDetected` per mismatch in report |
| `test_paper_repair_emits_metric` | `ReconciliationPaperRepair` emitted when `mismatch.repaired = True` |
| `test_live_alert_emits_metric` | `ReconciliationLiveAlert` emitted in live mode |
| `test_flush_called_after_run` | `flush()` awaited at end of `run()` |
| `test_metrics_not_emitted_when_no_mismatches` | Zero calls to any metrics method on clean scan |

#### `TestMISMetrics` (8 tests)

| Test | Validates |
|---|---|
| `test_positions_found_gauge_emitted` | `record_gauge("MISPositionsFound", N, ...)` after scan |
| `test_long_short_counts_emitted` | Separate LONG and SHORT gauges |
| `test_order_placed_counter_emitted` | `record_count("MISOrderPlaced", ...)` per close order |
| `test_order_rejected_emitted_on_exception` | `record_count("MISOrderRejected", ...)` on broker error |
| `test_confirmed_flat_counter_emitted` | `record_count("MISPositionConfirmedFlat", ...)` per fill confirmed |
| `test_duplicate_prevented_emitted` | `record_count("MISDuplicatesPrevented", ...)` when router returns False |
| `test_product_type_mis_counter` | `record_count("MISProductTypeMIS", ...)` per valid MIS order |
| `test_product_type_invalid_emits_critical` | `record_count("MISProductTypeInvalid", ...)` on wrong product type |

#### `TestLiveGateStructural` (5 tests)

| Test | Validates |
|---|---|
| `test_live_trading_enabled_false_in_service` | `service.py` constructs ExitOrderRouter with `live_trading_enabled=False` |
| `test_env_var_not_set_blocks_live` | `QE_LIVE_EXITS_ENABLED` absent → live blocked |
| `test_exit_router_blocks_live_by_default` | `ExitOrderRouter(mode=LIVE, live_trading_enabled=False).route(req)` returns `False` |
| `test_exit_router_live_logs_blocked_event` | Blocked live call emits `exit_router.live_blocked` log |
| `test_no_broker_calls_in_paper_session` | Source inspection: `_route_paper()` never calls `zerodha_broker.place_order` |

### 3.2 Extension to existing test files

No changes to existing test files in Phase 4. The new `test_phase4_metrics.py` is standalone.

---

## 4. File Change Summary

| File | Change type | Description |
|---|---|---|
| `services/execution_engine/monitors/trade_exit_engine.py` | Modify | Add `metrics` and `mode` params to `__init__`; add `record_count` calls in 3 methods |
| `services/execution_engine/reconciliation/reconciliation.py` | Modify | Add `metrics` param; add `record_count` calls in `run()` |
| `services/execution_engine/mis_square_off.py` | Modify | Add `metrics` param; add 9 `record_count`/`record_gauge` calls across 3 methods |
| `services/execution_engine/service.py` | Modify | Pass `mode=` to `TradeExitEngine` constructor (1 line) |
| `tests/unit/test_phase4_metrics.py` | Create | 26 new tests |
| `docs/operations/paper-trading-acceptance-checklist.md` | Create | Already done ✓ |
| `docs/operations/live-mode-gate-checklist.md` | Create | Already done ✓ |
| `docs/architecture/hybrid-trade-exit-and-mis-squareoff.md` | Modify | Add §16 Phase 4 detail |

**Files NOT changed:**
- `services/execution_engine/exit/exit_order_router.py` — live gate already in place, no new logic needed
- `services/shared/metrics/cloudwatch_metrics.py` — no changes needed, client is complete
- `services/shared/config/settings.py` — no new config needed

---

## 5. Risk Assessment

| Risk | Likelihood | Mitigation |
|---|---|---|
| Metrics calls add latency to exit hot path | Very low | All calls are `record_count()` (synchronous buffer append, O(1)); `flush()` is a background task |
| CloudWatch API failure blocks exit | Not possible | `flush()` never raises; failures are logged as WARNING and dropped |
| `mode` string added to TEE incorrect value | Low | Derived from same `paper_trading` flag used for ExitOrderRouter mode; tested |
| MIS `return_value=False` from router not checked | Existing gap | Phase 4 adds the check; `MISDuplicatesPrevented` metric is the enforcement |
| DRY-RUN metrics in tests → silent pass | Design | Tests inject `MagicMock()` — all calls are visible and asserted |

---

## 6. Open Questions for Approval

1. **Metric namespace split**: Should MIS/TEE/reconciliation metrics go into `QuantEmbrace/ExitManagement` rather than `QuantEmbrace/Trading`? Splitting allows separate CloudWatch dashboards and alarms per domain. Current plan: single `QuantEmbrace/Trading` namespace for simplicity.

2. **`MISDuplicatesPrevented` location**: Currently designed to live in `MISSquareOffManager`. Alternatively, it could live in `ExitOrderRouter` (emitted whenever `ConditionalCheckFailedException` is caught). Moving it to the router would also count TEE-vs-TEE duplicates (same symbol, two poll cycles). Which scope is preferred?

3. **`mode` on TEE**: TEE doesn't need to know its mode for exit routing (the router owns mode). Adding `mode` purely for metric dimensions. Alternative: omit `mode` from TEE metrics and let the metric consumer infer mode from service config. Current plan: include `mode` in dimensions for easier CloudWatch filtering.

4. **Paper acceptance session count**: The checklist requires 5 full sessions. If the first 4 are clean, should session 5 be mandatory? Current plan: yes, 5 sessions non-negotiable.

---

## 7. Implementation Order (if approved)

1. Add metrics to `PositionReconciliationService` (smallest change — 1 file)
2. Add metrics to `MISSquareOffManager` (including the `router.route() return value` check)
3. Add `mode` and metrics to `TradeExitEngine`
4. 1-line change to `service.py` (pass `mode=`)
5. Write `tests/unit/test_phase4_metrics.py`
6. Run full test suite (185 + 26 = 211 tests expected)
7. Update `hybrid-trade-exit-and-mis-squareoff.md` §16
