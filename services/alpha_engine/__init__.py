"""Alpha Engine — shadow-mode forecast generation + alpha research (ADR-031).

ADVISORY ONLY. This service generates and ranks alpha forecasts and persists
research artifacts. It MUST NOT place orders, run risk checks, maintain
positions, manage kill switches, or import a broker SDK. Its only output to the
live event bus is the ``alpha.opportunities`` shadow topic; it never publishes to
``signals.pending`` / ``signals.approved`` / ``orders.*``.

See ``docs``/``memory/decisions.md`` ADR-031 and the plan at
``.claude/plans/logical-swimming-wilkes.md``.
"""
