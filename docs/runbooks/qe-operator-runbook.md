# QuantEmbrace v2 (`qe`) — Operator Runbook

> **One page. This is the whole operating surface of the v2 engine (ADR-037/038).**
> No containers, no cluster, no message bus. One process, one config file, one journal.
> Advisory only. Backtesting recommends; a human promotes. Live trading remains BLOCKED.

## What replaced what

| You used to run… | Now run… |
|---|---|
| `docker-compose … + validate_session12 + 30 study scripts` | `python -m qe …` |
| `replay_delivery_book_forward.py --factor delivery` (monthly) | `python -m qe study --config configs/qe_delivery_book.yaml` |
| `run_delivery_walkforward.py` | `python -m qe study --config configs/qe_delivery_walkforward.yaml` |
| `run_delivery_paper_book.py --rebalance` | `python -m qe paper --config configs/qe_delivery_book_paper.yaml` |
| LiveCounters JSON + `paper_trading_monitor.py` | `python -m qe report --journal <path>` |

The v1 scripts still work and remain the **fallback** until the decommission gate passes
(`docs/runbooks/v1-decommission-runbook.md`). They are no longer the primary path.

## Monthly forward-book cadence (the one standing job)

```bash
# 1. Refresh the lake (unchanged — the downloaders are retained v1 tooling)
python scripts/backtest/download_bhavcopy.py            # → backtest-data/lake (NSE)
python scripts/backtest/download_us_eod.py              # → backtest-data/lake (US, ADR-041 P5)

# 2. Advance each forward book one month (research view, full provenance + report)
python -m qe study --config configs/qe_delivery_book.yaml
python -m qe study --config configs/qe_momentum_book.yaml
python -m qe study --config configs/qe_us_rplite_book.yaml     # ADR-041 P5, NOT YET ACTIVATED

# 2b. OPTIONAL cross-check (recommended monthly, NSE only — there is no v1 US book):
#     advance the v1 replay too — the gate checker then cross-checks qe vs v1 at the
#     same frontier and flags any mismatch.
python scripts/paper/replay_delivery_book_forward.py --factor delivery
python scripts/paper/replay_delivery_book_forward.py --factor momentum

# 3. Check the pre-registered forward gates (unchanged governance; reads the qe study
#    summaries directly since ADR-040 — `--source qe|v1|auto`, default auto)
python scripts/paper/check_forward_gate.py             # NSE, eligible ~Dec-2026
python scripts/paper/check_us_forward_gate.py          # US RPLITE, ADR-041 P5, NOT YET ACTIVATED
```

Every run writes: a journal (`journals/…jsonl`), a pinned data snapshot
(`backtest-data/lake/_snapshots/…`), and `reports/qe/<session>/report.md` + `summary.json`,
each stamped with the config hash + code SHA + snapshot id. Session validity is mechanical:
the journal's config hash must equal the approved config hash. No validation script needed.

**US RPLITE book status (ADR-041 P5, 2026-07-14):** built and tested (qe engine has full
US market support, paper==sim proven for the US book exactly as for NSE — see
`docs/strategy/us-qe-phase5-report.md`), but **not yet activated** — awaiting operator
approval. Once approved, `configs/qe_us_rplite_book.yaml` / `qe_us_rplite_book_paper.yaml`
join the cadence above on the same footing as the NSE books; first forward rebalance is
scheduled for 2026-07-31.

## Real-time paper session (run any day; rebalances follow the month-end schedule)

```bash
python -m qe paper --config configs/qe_delivery_book_paper.yaml   # defaults to latest lake date
python -m qe paper --config configs/qe_momentum_book_paper.yaml
python -m qe paper --config configs/qe_us_rplite_book_paper.yaml  # ADR-041 P5, NOT YET ACTIVATED
python -m qe report --journal journals/paper-<session>.jsonl
```

Paper is a backtest running in real time — byte-for-byte the same engine (proven: paper==sim
to ₹0.00). A session executes the completed month-end rebalances the book still owes — at that
month-end's own prices, exactly as sim schedules them (ADR-040) — and is otherwise **MTM-only**;
mid-month runs never trade. A month-end is owed only once the month is provably over (a
later-month row in the lake, or the wall clock past the month), so the normal cadence is:
refresh the lake after month-end, then run the session. Idempotent per month, resumes the
persisted book, and **fails closed** on stale data or a drawdown breach.

## Kill switch

```bash
python -m qe kill status
python -m qe kill activate --reason "…"      # halts all order emission (idempotent, no re-fire)
python -m qe kill deactivate
```

One in-process state machine, one flag file. An active kill blocks every order emission and is
journaled; auto-triggers (drawdown past `risk.max_drawdown_frac`, data staleness past
`risk.max_data_age_days`) trip it automatically.

## Guardrails (unchanged from v1 governance)

- **Live BLOCKED.** The live broker is unconstructible without an M6 gate token. Paper uses
  `PaperBroker`, which cannot reach a real venue.
- **Capital protection > trade count > profit.** Full NSE statutory cost stack in every run.
- **Backtesting recommends; a human promotes.** No auto-promotion, ever.
