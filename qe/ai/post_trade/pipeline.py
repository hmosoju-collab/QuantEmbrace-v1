"""Post-trade analysis run (ADR-043 P7) + knowledge-time-stamped lessons.

    engine journal (read-only) ──► completed trades ──► deterministic review
         + research journals (signal at entry, if any)   + LLM lesson (narrative)
                                                          └─► reports/qe-ai/post_trade/<session>/

The engine journal is only ever read — reviews never rewrite a trade record.
Each review is knowable at its trade's exit close; ``lessons_known_at`` returns
only reviews whose knowledge_ts ≤ a cutoff, which is how later research may use
past lessons without look-ahead (the safe form of TradingAgents' reflection).
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
import uuid

from qe.ai.agents import AgentContext, run_agent
from qe.ai.agents.post_trade import SPEC
from qe.ai.config import ResearchRunConfig
from qe.ai.llm import CircuitBreaker, LLMClient, LLMGateway, ResponseCache, RunBudget, build_client
from qe.ai.models import ComponentStatus, Evidence
from qe.ai.orchestration.journal import ResearchJournal
from qe.ai.paths import AI_JOURNAL_DIR, AI_REPORT_DIR, safe_write_path
from qe.ai.post_trade.review import PostTradeReview, facts, nan_safe, review_id
from qe.ai.post_trade.trades import Trade, completed_trades, open_positions
from qe.ai.reporting import ADVISORY_BANNER, load_research_journal
from qe.ai.tools import ResearchData, load_research_data
from qe.config import RunConfig
from qe.journal import JournalError, read_header
from qe.version import code_version

JOURNAL_MODE = "ai-post-trade"
PANEL_WARMUP_DAYS = 730


@dataclass(frozen=True)
class PostTradeRun:
    run_id: str
    journal_path: Path
    out_dir: Path | None
    reviews: tuple[PostTradeReview, ...]
    skipped: dict[str, int]
    open_positions: int


def _signals_at_entry(base_dir: Path) -> dict[tuple[date, str], tuple[float | None, bool]]:
    """(decision_date, symbol) -> (ai_score, contaminated) from the FIRST research
    run per decision date (same rule as the shadow gate: re-runs never override)."""
    out: dict[tuple[date, str], tuple[float | None, bool]] = {}
    first_run: dict[date, str] = {}
    for path in sorted((base_dir / AI_JOURNAL_DIR).glob("ai-*.jsonl")):
        try:
            view = load_research_journal(path)
        except (ValueError, JournalError, KeyError):
            continue
        d = date.fromisoformat(view.manifest["decision_date"])
        sid = view.header["session_id"]
        if d in first_run and first_run[d] < sid:
            continue
        first_run[d] = sid
        for s in view.signals:
            out[(d, s.symbol)] = (s.ai_score, s.contamination_risk)
    return out


def _evidence(f: dict, at: datetime) -> tuple[Evidence, ...]:
    meanings = {
        "gross_return": "price return, entry to exit",
        "net_return": "return net of allocated costs",
        "benchmark_return": "EW liquid-universe return over the hold",
        "excess_return": "net return minus benchmark",
        "mfe": "max favourable excursion vs entry",
        "mae": "max adverse excursion vs entry",
        "cost_bps": "round-trip costs, bps of buy notional",
        "holding_days": "calendar days held",
        "regime_at_entry": "PIT market regime at entry",
        "regime_accuracy": "code: regime call vs benchmark direction",
        "entry_quality": "code: entry vs first 10 sessions' range",
        "exit_quality": "code: share of max favourable excursion captured",
        "execution_quality": "code: cost bucket",
        "quant_thesis": "code: did the factor pick beat the benchmark",
        "ai_thesis": "code: did the AI view at entry match the outcome",
        "risk_event": "code: large adverse excursion",
    }
    items = []
    for name, meaning in meanings.items():
        v = nan_safe(f.get(name))
        if v is None or v == "":
            continue
        items.append(
            Evidence(
                evidence_id=f"trade.{name}",
                tool="trade",
                symbol=None,
                knowledge_ts=at,
                value=round(v, 6) if isinstance(v, float) else v,
                summary=meaning,
            )
        )
    return tuple(items)


def run_post_trade(
    engine_journal: str | Path,
    cfg: ResearchRunConfig,
    *,
    base_dir: str | Path = ".",
    client: LLMClient | None = None,
    allow_spend: bool = False,
    data: ResearchData | None = None,
    max_trades: int = 20,
) -> PostTradeRun:
    base_dir = Path(base_dir)
    header = read_header(engine_journal)
    if header.get("mode") in ("ai-research", "ai-hypotheses", JOURNAL_MODE):
        raise ValueError(f"{engine_journal} is not an engine (sim/paper) journal")
    book = RunConfig.model_validate(header["config"])
    market = book.universe.market
    top_n = book.strategy.top_n if book.strategy and book.strategy.kind == "factor_book" else 200
    trades = completed_trades(engine_journal)
    trades = sorted(trades, key=lambda t: (t.close_date, t.symbol))[-max_trades:]
    held = len(open_positions(engine_journal))
    client = client or build_client(cfg, allow_spend=allow_spend)
    if trades and data is None:
        start = min(t.open_date for t in trades) - timedelta(days=PANEL_WARMUP_DAYS)
        data = load_research_data(book, max(t.close_date for t in trades), base_dir, start=start)
    signals = _signals_at_entry(base_dir)

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"pt-{header['session_id'][:40]}-{stamp}-{uuid.uuid4().hex[:6]}"
    reviews: list[PostTradeReview] = []
    skipped: dict[str, int] = {}
    with ResearchJournal(base_dir, run_id) as journal:
        journal.start(
            run_id=run_id,
            config=cfg,
            code_sha=code_version(base_dir),
            snapshot_id=data.snapshot_id if data else "none",
            mode=JOURNAL_MODE,
        )
        journal.event(
            "POST_TRADE_MANIFEST",
            {
                "engine_session": header["session_id"],
                "n_trades": len(trades),
                "open_positions": held,
            },
        )
        gateway = LLMGateway(
            client,
            budget=RunBudget(cfg.budget.max_run_tokens),
            breaker=CircuitBreaker(cfg.budget.breaker_threshold),
            cache=ResponseCache(base_dir) if cfg.use_cache else None,
            max_retries=cfg.budget.max_retries,
            sink=journal.event,
        )
        ctx = AgentContext(
            gateway=gateway,
            quick=cfg.quick_model,
            deep=cfg.deep_model,
            market=market,
            max_tokens=cfg.budget.max_tokens_per_call,
            temperature=cfg.temperature,
            max_retries=cfg.budget.max_retries,
            mask_identifiers=cfg.mask_identifiers,
        )
        for t in trades:
            ai = signals.get((t.open_date, t.symbol))
            try:
                f = facts(t, data.panel, market, top_n, ai[0] if ai else None)
            except ValueError as exc:
                reason = str(exc).split(":", 1)[-1].strip()
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            run = run_agent(SPEC, ctx, symbol=t.symbol, evidence=_evidence(f, f["knowledge_ts"]))
            out = run.output
            review = PostTradeReview(
                review_id=review_id(t),
                source_session=t.session_id,
                symbol=t.symbol,
                open_date=t.open_date,
                close_date=t.close_date,
                qty_bought=t.qty_bought,
                entry_price=round(t.entry_price, 4),
                exit_price=round(t.exit_price, 4),
                **{
                    k: f[k]
                    for k in (
                        "gross_return",
                        "net_return",
                        "benchmark_return",
                        "excess_return",
                        "mfe",
                        "mae",
                        "cost_bps",
                        "regime_at_entry",
                        "regime_accuracy",
                        "entry_quality",
                        "exit_quality",
                        "execution_quality",
                        "quant_thesis",
                        "ai_thesis",
                        "risk_event",
                        "knowledge_ts",
                    )
                },
                ai_score_at_entry=ai[0] if ai else None,
                ai_contaminated=ai[1] if ai else None,
                unexpected_event=out.unexpected_event if out else "",
                lesson=out.lesson if out else "",
                lesson_status=run.observation.status,
                model_id=run.observation.model_id,
                prompt_version=SPEC.prompt_version,
            )
            journal.event("POST_TRADE_REVIEW", review.model_dump(mode="json"))
            reviews.append(review)
        journal.end("OK", {"n_reviews": len(reviews), "skipped": skipped})

    out_dir = None
    if reviews:
        sess = header["session_id"]
        md = safe_write_path(base_dir, AI_REPORT_DIR / "post_trade" / sess / "report.md")
        md.parent.mkdir(parents=True, exist_ok=True)
        safe_write_path(base_dir, AI_REPORT_DIR / "post_trade" / sess / "reviews.jsonl").write_text(
            "".join(json.dumps(r.model_dump(mode="json"), sort_keys=True) + "\n" for r in reviews)
        )
        md.write_text(render(reviews, sess, held))
        out_dir = md.parent
    return PostTradeRun(run_id, journal.path, out_dir, tuple(reviews), skipped, held)


def lessons_known_at(
    base_dir: str | Path, cutoff: datetime, limit: int = 10
) -> list[PostTradeReview]:
    """Reviews knowable at ``cutoff`` (knowledge_ts ≤ cutoff), newest first — the
    only way past lessons may reach a later research prompt."""
    root = Path(base_dir) / AI_REPORT_DIR / "post_trade"
    found: dict[str, PostTradeReview] = {}
    for path in sorted(root.glob("*/reviews.jsonl")) if root.exists() else []:
        for line in path.read_text().splitlines():
            if line.strip():
                r = PostTradeReview.model_validate_json(line)
                if r.knowledge_ts <= cutoff and r.lesson_status is ComponentStatus.OK:
                    found[r.review_id] = r
    return sorted(found.values(), key=lambda r: r.knowledge_ts, reverse=True)[:limit]


def _pct(v: float | None) -> str:
    return "—" if v is None else f"{v:+.1%}"


def render(reviews: list[PostTradeReview], session: str, held: int) -> str:
    n = len(reviews)
    confirmed = sum(r.quant_thesis == "CONFIRMED" for r in reviews)
    lines = [
        f"# Post-trade review — `{session}`",
        "",
        ADVISORY_BANNER,
        "",
        f"{n} completed trade(s) reviewed ({held} position(s) still open, not reviewed). "
        f"Factor thesis confirmed (beat the EW benchmark) in {confirmed}/{n}. Classifications "
        "are computed by code; the lesson column is the model's narrative.",
        "",
        "| Symbol | Held | Net | Excess | MAE | Entry | Exit | Regime call | Cost | AI thesis | Lesson |",
        "|---|---|---:|---:|---:|---|---|---|---|---|---|",
    ]
    for r in reviews:
        lines.append(
            f"| {r.symbol} | {r.open_date} → {r.close_date} | {_pct(r.net_return)} | "
            f"{_pct(r.excess_return)} | {_pct(r.mae)} | {r.entry_quality} | {r.exit_quality} | "
            f"{r.regime_accuracy} | {r.execution_quality} | {r.ai_thesis} | "
            f"{r.lesson.replace('|', '/') or '(' + r.lesson_status + ')'} |"
        )
    return "\n".join(lines) + "\n"


__all__ = ["PostTradeRun", "Trade", "lessons_known_at", "run_post_trade"]
