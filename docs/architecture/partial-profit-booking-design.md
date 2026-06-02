# Partial Profit Booking — Design & TODO Structure

**Status:** PHASE 3.1 — DESIGN ONLY (implementation deferred to Phase 4)  
**Author:** Chief Architect  
**Date:** 2026-05-25  
**Depends on:** Phase 3 complete (trailing stop, reconciliation, MIS/TEE integration)

---

## 1. What Partial Profit Booking Is

Partial profit booking (PPB) allows a position to be partially closed when price reaches a defined target level, while keeping the remaining portion open with the stop moved to breakeven (or better). This is distinct from full take-profit in that the position survives the trigger — it continues running.

Example — LONG MARUTI, qty=10, entry=14000:
```
At entry:       qty=10, stop=13650, tp=None, PPB level=14500 (50%)
PPB fires:      qty=5 exits at 14500 (profit locked)
After PPB:      qty=5 remains open, stop moved to 14000 (breakeven)
Later:          remaining qty=5 hits new trailing stop or MIS
```

---

## 2. Why This Is Deferred

Partial profit booking requires:
1. **Partial fill application** — `apply_fill_to_position()` must handle `filled_qty < abs(current_qty)` and leave a non-zero remainder with updated `avg_price`
2. **Split exit idempotency** — two different exit orders for the same position on the same day (`PPB` + `STOP_LOSS`). The current idempotency key format (`EXIT-{symbol}-{market}-{trigger}-{date}`) is unique per trigger type, so PPB and STOP_LOSS keys would not collide — but the DynamoDB `exit_order_id` field is a single scalar, not a list
3. **Stop mutation after partial close** — moving stop to breakeven after PPB fires is a state transition that must be atomic with the partial fill confirmation
4. **MIS interaction** — at 15:05 MIS fires on `abs(qty) > 0`; after PPB the remaining qty is still > 0, so MIS will correctly close it. This is the correct behavior.

These interactions require careful design and testing before implementation. Phase 3's focus is proving TEE+MIS idempotency is airtight first.

---

## 3. Design Decisions

### 3.1 Exit Order ID Structure for Partials

The `exit_order_id` field on the position record is currently a **single scalar**. PPB fires a partial exit; the position remains open and can still receive a STOP_LOSS exit later.

**Option A — Two separate `exit_order_id` fields:**
```
exit_order_id          String   in-flight FULL close (SL / TP / MIS / KS)
ppb_exit_order_id      String   in-flight PARTIAL close (PPB only)
```
Both can be set simultaneously. MIS and TEE check `exit_order_id` only; PPB logic checks `ppb_exit_order_id` only.

**Option B — Exit order ID list:**
```
exit_order_ids         List[String]   all in-flight exit orders (partial or full)
```
Complex to query with DynamoDB condition expressions.

**Decision: Option A** — Two separate fields. Simpler conditional writes, no list semantics needed in DynamoDB.

### 3.2 PPB Configuration (per-position)

PPB parameters are set at `attach_exit_policy()` time alongside `stop_price` and `take_profit`:

```python
await order_manager.attach_exit_policy(
    symbol=symbol,
    stop_price=approved.stop_loss,
    take_profit=approved.take_profit,
    ppb_level=approved.ppb_level,         # price at which PPB fires (None = disabled)
    ppb_fraction=approved.ppb_fraction,   # fraction to close (0.5 = 50%)
    ppb_move_stop_to_breakeven=True,      # move stop to avg_entry_price after PPB
)
```

DynamoDB fields added:
- `ppb_level` (Number) — trigger price; absent = PPB disabled for this position
- `ppb_fraction` (Number) — 0 < x < 1; default 0.5
- `ppb_fired` (Boolean) — set True after PPB fires; prevents re-firing
- `ppb_exit_order_id` (String) — in-flight PPB exit order ID

### 3.3 PPB Trigger Evaluation (in TEE)

Added to `_evaluate_exit_conditions()` after SL/TP checks, before trailing management:

```python
# PPB check — only if not yet fired and not mid-full-exit
if (
    ppb_level is not None
    and not ppb_fired
    and exit_order_id is None          # no full exit in-flight
    and ppb_exit_order_id is None      # no PPB exit in-flight
):
    if _ppb_triggered(direction, last_price, ppb_level):
        await self._fire_partial_exit(position, last_price)
        return  # do not also fire SL/TP on same cycle
```

PPB trigger logic (symmetric with TP):
```python
@staticmethod
def _ppb_triggered(direction: str, last_price: float, ppb_level: float) -> bool:
    if direction == "LONG":
        return last_price >= ppb_level
    elif direction == "SHORT":
        return last_price <= ppb_level
    return False
```

### 3.4 PPB Fire Sequence

```
_fire_partial_exit(position, last_price)
    │
    ▼
1. Compute close_qty = floor(abs(quantity) * ppb_fraction)
   (minimum 1 unit; if close_qty == abs(quantity), treat as full exit)
    │
    ▼
2. Build ExitOrderRequest(
       close_qty=close_qty,
       trigger_type=ExitTriggerType.PARTIAL_PROFIT,
       is_partial=True,
   )
    │
    ▼
3. ExitOrderRouter.route_partial(request)
   Conditional write:
     ConditionExpression: "attribute_not_exists(ppb_exit_order_id) AND direction <> :flat"
     UpdateExpression: "SET ppb_exit_order_id = :ppb_oid, ppb_fired = :true"
   If condition fails → skip (already in-flight)
    │
    ▼
4. Paper mode: apply_fill_to_position(filled_qty=close_qty)
   Remaining qty = old_qty - close_qty (still > 0 for LONG)
   direction stays LONG (not FLAT)
    │
    ▼
5. If ppb_move_stop_to_breakeven:
       _write_stop_price(symbol, new_stop=avg_entry_price)
       log tee.ppb_stop_moved_to_breakeven
    │
    ▼
6. Clear ppb_exit_order_id (fill confirmed)
   Set ppb_fired = True (permanent — no re-fire)
```

### 3.5 Idempotency After PPB

After PPB fires and position is partially closed:
- `ppb_fired = True` → TEE never fires PPB again on this position
- `exit_order_id` is still NULL → full SL/TP/MIS exits can still fire on remaining qty
- `ppb_exit_order_id` is cleared after fill confirmation → slot is available but `ppb_fired` blocks reuse
- MIS at 15:05 sees `abs(remaining_qty) > 0` → fires close order via `exit_order_id` slot (not `ppb_exit_order_id`)

### 3.6 ExitTriggerType Extension

```python
class ExitTriggerType(str, Enum):
    STOP_LOSS        = "STOP_LOSS"
    TAKE_PROFIT      = "TAKE_PROFIT"
    TRAILING         = "TRAILING"
    MIS_CLOSE        = "MIS_CLOSE"
    KILL_SWITCH      = "KILL_SWITCH"
    PARTIAL_PROFIT   = "PARTIAL_PROFIT"   # NEW
```

Idempotency key for PPB:
```
EXIT-{symbol}-{market}-PARTIAL_PROFIT-{session_date_ist}
```
This key is stored in `ppb_exit_order_id`, not `exit_order_id`. The namespaces do not collide.

---

## 4. DynamoDB Schema Additions

New fields on the `positions` record (in addition to Phase 2 fields):

| Field | Type | Description |
|---|---|---|
| `ppb_level` | Number | PPB trigger price; absent = disabled |
| `ppb_fraction` | Number | Fraction to close at PPB trigger (0.0–1.0) |
| `ppb_fired` | Boolean | Set True after PPB fires; prevents re-fire |
| `ppb_exit_order_id` | String | In-flight PPB partial exit order ID |
| `exit_state` | String | TRAILING_ACTIVE, PPB_FIRED, EXIT_POLICY_ATTACHED, etc. |

`exit_state` is updated to reflect the current lifecycle stage:
- `EXIT_POLICY_ATTACHED` — entry filled, stop+tp attached, no PPB yet
- `TRAILING_ACTIVE` — trailing stop is updating (Phase 3)
- `PPB_FIRED` — partial profit taken, stop moved to breakeven or better (Phase 4)
- (null/absent) — position is FLAT

---

## 5. Interaction Matrix

| Concurrent state | Safe? | Reason |
|---|---|---|
| PPB in-flight + SL fires | YES | Different DynamoDB fields (`ppb_exit_order_id` vs `exit_order_id`); SL fires full close |
| PPB in-flight + MIS fires | YES | MIS uses `exit_order_id` slot; both can proceed; whichever fills first sets direction=FLAT |
| PPB in-flight + kill switch | YES | Kill switch uses `exit_order_id` slot; PPB can complete concurrently or race — net effect is position closed |
| PPB fired + trailing active | YES | After PPB, stop moves to breakeven; trailing may then advance further; not a conflict |
| PPB fires twice | NO (by design) | `ppb_fired = True` blocks re-evaluation |

---

## 6. TODO Structure (Phase 4 Implementation)

> These are implementation tasks, not yet scheduled. Implement only after Phase 3 paper acceptance criteria are met.

### 6.1 Core Implementation

- [ ] **`services/execution_engine/models/exit_request.py`** — add `is_partial: bool = False` and `ExitTriggerType.PARTIAL_PROFIT` to enum
- [ ] **`services/execution_engine/exit/exit_order_router.py`** — add `route_partial(request)` method using `ppb_exit_order_id` conditional write
- [ ] **`services/execution_engine/monitors/trade_exit_engine.py`**:
  - `_parse_position()` — extract `ppb_level`, `ppb_fraction`, `ppb_fired`, `ppb_exit_order_id` from DynamoDB item
  - `_evaluate_exit_conditions()` — add PPB check block before trailing management
  - `_ppb_triggered()` — static method, symmetric with `_take_profit_triggered()`
  - `_fire_partial_exit()` — async, builds `ExitOrderRequest(is_partial=True)`, calls `route_partial()`
  - `_write_stop_price()` — DynamoDB `update_item` SET `stop_price = :new_stop` (reuse or extend `_write_trailing_stop`)
- [ ] **`services/execution_engine/order_manager.py`**:
  - `apply_fill_to_position()` — handle partial fill: `new_qty = old_qty - filled_qty` remains non-zero; direction stays LONG/SHORT; update `avg_price` if partial
  - `attach_exit_policy()` — accept `ppb_level`, `ppb_fraction`, `ppb_move_stop_to_breakeven` params; write to DynamoDB

### 6.2 Startup Reconciliation Extension

- [ ] **`services/execution_engine/reconciliation/reconciliation.py`** — add `PPB_PARTIAL_ORPHAN` mismatch type: detects `ppb_fired=True` but `qty > ppb_fraction * original_qty` (position didn't shrink as expected after PPB fill)

### 6.3 Tests

- [ ] `tests/unit/test_trade_exit_engine.py` — `TestPartialProfitBooking` class:
  - `test_ppb_fires_when_price_meets_level_long`
  - `test_ppb_fires_when_price_meets_level_short`
  - `test_ppb_does_not_fire_below_level_long`
  - `test_ppb_not_refired_when_ppb_fired_true`
  - `test_ppb_skipped_when_exit_in_flight`
  - `test_ppb_moves_stop_to_breakeven`
  - `test_ppb_does_not_affect_full_exit_slot`
  - `test_partial_qty_computed_correctly` (floor(qty * fraction))
  - `test_ppb_min_qty_is_1_unit`

- [ ] `tests/unit/test_exit_order_router.py` — extend `TestExitOrderRouter`:
  - `test_route_partial_uses_ppb_exit_order_id_field`
  - `test_route_partial_idempotency_ppb_slot`
  - `test_route_full_and_partial_can_coexist`

- [ ] `tests/integration/test_ppb_mis_integration.py`:
  - `test_ppb_fires_then_sl_closes_remainder`
  - `test_ppb_fires_then_mis_closes_remainder`
  - `test_ppb_and_mis_concurrent_race`

### 6.4 Observability

New log fields to add at PPB fire time:
```python
logger.info(
    "tee.partial_profit_fired",
    symbol=symbol,
    direction=direction,
    ppb_level=ppb_level,
    close_qty=close_qty,
    remaining_qty=remaining_qty,
    new_stop=new_stop,
    product_type="MIS",
    mode=self._mode,
)
```

New metrics:
- `trade_exit.partial_profit_fired_total` — Counter, labels: `direction`, `mode`
- `trade_exit.ppb_stop_moved_total` — Counter, labels: `mode`

### 6.5 Acceptance Criteria (Paper)

Before enabling PPB in paper sessions:
- [ ] PPB fires exactly once per position per session (verified via `ppb_fired` flag in DynamoDB)
- [ ] Remaining quantity reduces by expected `ppb_fraction` after PPB fill confirmed
- [ ] Stop price in DynamoDB equals `avg_entry_price` (breakeven) after PPB fires
- [ ] Subsequent SL/TP/MIS fires on remaining quantity (not blocked by PPB state)
- [ ] No duplicate exit orders in orders table
- [ ] `orders.events` contains both PPB fill event and subsequent close event

---

## 7. Risks

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| PPB fill partial but DynamoDB write fails | Low | Position qty incorrect | `apply_fill_to_position` uses conditional write; retry on failure; reconciliation detects `PPB_PARTIAL_ORPHAN` |
| Stop moved to breakeven then trailing loosens it | Design | Medium | `compute_trailing_stop()` for LONG uses `max(current_stop, ...)` — it will never move stop below breakeven if breakeven > original stop |
| MIS fires during PPB fill in-flight | Low | Two orders for partial close | MIS checks `exit_order_id` (full slot), not `ppb_exit_order_id`; they use different slots — both proceed; net qty goes to 0 faster, not a duplication risk |
| `ppb_fraction` rounds to 0 units | Low | PPB silently no-ops | Guard: `if close_qty < 1: logger.warning(...); return` |

---

## 8. Deferred Enhancements (Phase 5+)

- Multiple PPB levels (e.g., close 25% at TP1, 25% more at TP2, hold 50% to trailing)
- PPB for US equities with fractional share support (`ppb_fraction` can yield fractional qty)
- Strategy-configurable PPB parameters per signal (currently hardcoded at `attach_exit_policy`)
- PPB applied to F&O (lot-size aware: minimum close = 1 lot)
