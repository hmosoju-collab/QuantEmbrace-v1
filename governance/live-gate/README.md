# Live Gate — evidence artifacts

The v2 live path is blocked **by construction**: `qe.live_gate.mint_live_gate_token`
refuses to mint a `LiveGateToken` unless every pre-registered precondition passes, and
`LiveBroker` cannot be constructed without a valid token *and* an explicitly-supplied broker
client. `python -m qe live --config <cfg>` runs the ceremony and prints the check ledger.

**As of 2026-07-06 the gate correctly REFUSES** (forward books have ~5/12 months, 0 clean qe
paper sessions, lake stale, no human artifacts). This directory is where the human-produced
evidence lands when the time comes. Backtesting recommends; a human promotes.

## Files a human creates here (none exist yet — that is the point)

### `forward-gate-pass.json` — after `check_forward_gate.py` reports PASS (~Dec-2026+)
```json
{ "result": "PASS", "config_hash": "<the exact RunConfig hash going live>",
  "recorded_by": "hari", "date": "2026-12-31", "note": "FFG cleared; see report path" }
```

### `operator-approval.json` — explicit, config-bound human sign-off
```json
{ "approved": true, "config_hash": "<same RunConfig hash>", "operator": "hari",
  "date": "2026-12-31", "note": "reviewed forward record + drills; approve tiny-capital pilot" }
```

The ceremony independently re-verifies (≥12 forward months in qe study summaries, ≥3 clean qe
paper sessions, kill clear, lake fresh) — the artifacts alone are never trusted. Minted
`token-*.json` files are ephemeral (24h TTL, config-bound) and git-ignored.

## The gate is never relaxed to force a pass

Thresholds mirror the pre-registered Forward Factor Gate (`docs/live-readiness/forward-factor-validation-gate.md`).
Changing them to admit a book that hasn't earned it defeats the entire research program's lesson.
