"""The research graph — an explicit DAG, deterministic given its inputs.

    load (PIT panel + snapshot) → regime tool → regime analyst      (once per run)
    per symbol:  tools → analysts → [bull <-> bear] x rounds → critic → synthesizer
                 → ResearchSignal v1 (evidence attached + validated by code)

Every step is journaled to ``journals/ai/<run_id>.jsonl``. A symbol that fails
(including a point-in-time violation, which invalidates its signal) is
journaled as SYMBOL_FAILED and the run continues; an LLM failure never raises —
it becomes a component status. Nothing here touches the trading engine.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
import uuid

from qe.ai.agents import ANALYSTS, DEBATERS, AgentContext, run_agent, run_analyst
from qe.ai.config import ResearchRunConfig
from qe.ai.guardrails import redact
from qe.ai.llm import (
    CircuitBreaker,
    LLMClient,
    LLMGateway,
    ResponseCache,
    RunBudget,
    build_client,
)
from qe.ai.models import (
    DIRECTIONAL,
    AgentObservation,
    ComponentStatus,
    LookaheadViolation,
    RegimeSummary,
    ResearchReport,
    ResearchSignal,
    ai_score_of,
    is_contaminated,
)
from qe.ai.orchestration.debate import debate_status, run_debate
from qe.ai.orchestration.journal import ResearchJournal
from qe.ai.orchestration.modes import MODES, SYMBOL_ANALYSTS, ModeSpec
from qe.ai.orchestration.state import DebateTurn, SymbolState, observation_block, ok
from qe.ai.tools import (
    TOOLS_VERSION,
    QuantSpec,
    QuantView,
    RegimeReading,
    ResearchData,
    ResearchDataAPI,
    ToolResult,
    fundamentals,
    knowledge_ts,
    load_research_data,
    news,
    quant,
    quant_view,
    regime,
    risk,
    sentiment,
    technical,
)
from qe.config import RunConfig
from qe.version import code_version

SKIPPED = ComponentStatus.SKIPPED


@dataclass(frozen=True)
class ResearchRunResult:
    run_id: str
    journal_path: Path
    report: ResearchReport


def research_symbols(
    cfg: ResearchRunConfig, view: QuantView | None, extra: tuple[str, ...] = ()
) -> list[str]:
    """The engine's own basket first (what the book would hold), then any
    explicitly requested names; de-duplicated and capped (cost control)."""
    ordered = [*(view.basket if view else ()), *(cfg.symbols or ()), *extra]
    return list(dict.fromkeys(ordered))[: cfg.max_symbols]


def _new_run_id(cfg: ResearchRunConfig) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return f"ai-{cfg.name}-{stamp}-{uuid.uuid4().hex[:6]}-{cfg.short_hash}"


def run_research(
    cfg: ResearchRunConfig,
    *,
    as_of: date,
    base_dir: str | Path = ".",
    client: LLMClient | None = None,
    allow_spend: bool = False,
    book: RunConfig | None = None,
    data: ResearchData | None = None,
    extra_symbols: tuple[str, ...] = (),
) -> ResearchRunResult:
    """``client`` / ``book`` / ``data`` injection is for tests; a real run
    builds the client under the spend guard and loads the lake + snapshot."""
    base_dir = Path(base_dir)
    book = book or RunConfig.from_yaml(base_dir / cfg.book_config)
    market = book.universe.market
    data = data or load_research_data(book, as_of, base_dir)
    api = ResearchDataAPI.at(
        data.panel, knowledge_ts(as_of, market), market, data.index_bars, data.corpus
    )
    spec = QuantSpec.from_book(book)
    view = quant_view(api, spec) if spec else None
    symbols = research_symbols(cfg, view, extra_symbols)
    mode = MODES[cfg.research_mode]
    client = client or build_client(cfg, allow_spend=allow_spend)

    run_id = _new_run_id(cfg)
    with ResearchJournal(base_dir, run_id) as journal:
        journal.start(
            run_id=run_id, config=cfg, code_sha=code_version(base_dir), snapshot_id=data.snapshot_id
        )
        cutoffs = _cutoffs_used(cfg, mode)
        journal.event(
            "RUN_MANIFEST",
            {
                "as_of": as_of.isoformat(),
                "decision_date": api.decision_date.isoformat(),
                "information_cutoff": api.cutoff.isoformat(),
                "market": market,
                "book_config": cfg.book_config,
                "book_config_hash": book.config_hash(),
                "quant_spec": spec.__dict__ if spec else None,
                "symbols": symbols,
                "research_mode": cfg.research_mode,
                "mode": mode.__dict__,
                "backend": client.name,
                "models": {
                    "quick": cfg.quick_model.model_dump(mode="json"),
                    "deep": cfg.deep_model.model_dump(mode="json"),
                },
                "knowledge_cutoffs_used": {
                    k: v.isoformat() if v else None for k, v in cutoffs.items()
                },
                "prompts": {
                    s.prompt_version: s.prompt_hash
                    for s in (*ANALYSTS.values(), *DEBATERS.values())
                },
                "tools_version": TOOLS_VERSION,
                "corpus_hash": data.corpus.corpus_hash,
                "budget": cfg.budget.model_dump(mode="json"),
                "seed": cfg.seed,
                "mask_identifiers": cfg.mask_identifiers,
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

        regime_result, reading = regime(api)
        journal.event("TOOL_CALL", regime_result.journal_view())
        regime_run = run_analyst(ANALYSTS["regime"], ctx, symbol=None, results=[regime_result])
        journal.event("AGENT_OBSERVATION", regime_run.observation.model_dump(mode="json"))

        signals: list[ResearchSignal] = []
        failed: list[str] = []
        for symbol in symbols:
            trace_id = uuid.uuid4().hex
            try:
                state = _research_symbol(
                    api, ctx, journal, mode, symbol, trace_id, spec, view, regime_result, regime_run
                )
                signal = _build_signal(
                    cfg,
                    api,
                    mode,
                    state,
                    run_id,
                    reading,
                    regime_run.observation,
                    cutoffs,
                    client.name,
                )
            except Exception as exc:  # one symbol's failure must never end the run
                failed.append(symbol)
                journal.event(
                    "SYMBOL_FAILED",
                    {
                        "symbol": symbol,
                        "trace_id": trace_id,
                        "error": redact(f"{type(exc).__name__}: {exc}")[:300],
                    },
                )
                continue
            journal.event("RESEARCH_SIGNAL", signal.model_dump(mode="json"))
            signals.append(signal)

        stats = gateway.stats
        report = ResearchReport(
            research_id=run_id,
            as_of=as_of,
            information_cutoff=api.cutoff,
            market=market,
            research_mode=cfg.research_mode,
            config_hash=cfg.config_hash(),
            data_snapshot_id=data.snapshot_id,
            code_sha=code_version(base_dir),
            regime=RegimeSummary(
                label=reading.label, value=reading.value, confidence=reading.confidence
            ),
            signals=tuple(signals),
            failed_symbols=tuple(failed),
            llm_calls=stats.calls,
            cache_hits=stats.cache_hits,
            input_tokens=stats.input_tokens,
            output_tokens=stats.output_tokens,
        )
        journal.end(
            "OK",
            {
                "n_symbols": len(symbols),
                "n_signals": len(signals),
                "failed_symbols": failed,
                "llm_calls": stats.calls,
                "cache_hits": stats.cache_hits,
                "input_tokens": stats.input_tokens,
                "output_tokens": stats.output_tokens,
                "llm_failures": stats.failures,
                "by_status": stats.by_status,
            },
        )
    return ResearchRunResult(run_id, journal.path, report)


def _cutoffs_used(cfg: ResearchRunConfig, mode: ModeSpec) -> dict[str, date | None]:
    """Knowledge cutoffs of every model this mode can call (conservative)."""
    out = {cfg.quick_model.model_id: cfg.quick_model.knowledge_cutoff}
    if mode.synthesizer_tier == "deep":
        out[cfg.deep_model.model_id] = cfg.deep_model.knowledge_cutoff
    return out


def _research_symbol(
    api: ResearchDataAPI,
    ctx: AgentContext,
    journal: ResearchJournal,
    mode: ModeSpec,
    symbol: str,
    trace_id: str,
    spec: QuantSpec | None,
    view: QuantView | None,
    regime_result: ToolResult,
    regime_run,
) -> SymbolState:
    top_n = spec.top_n if spec else 200
    tools = {
        "technical": (technical(api, symbol), quant(api, symbol, spec, view)),
        "risk": (risk(api, symbol, top_n),),
        "fundamental": (fundamentals(symbol),),
        "news": (news(api, symbol),),
        "sentiment": (sentiment(symbol),),
    }
    for results in tools.values():
        for r in results:
            journal.event("TOOL_CALL", {"trace_id": trace_id, **r.journal_view()})
            _assert_pit(r, api)

    analysts = {}
    for agent in mode.analysts:
        run = run_analyst(ANALYSTS[agent], ctx, symbol=symbol, results=tools[agent])
        journal.event(
            "AGENT_OBSERVATION", {"trace_id": trace_id, **run.observation.model_dump(mode="json")}
        )
        analysts[agent] = run
    market_evidence = regime_result.evidence if regime_result.ok else ()
    state = SymbolState(symbol, trace_id, tools, market_evidence, analysts)

    evidence = state.evidence()
    blocks = [
        (f"analyst/{a}", observation_block(r.observation)) for a, r in analysts.items() if ok(r)
    ]
    if ok(regime_run):
        blocks.append(("analyst/regime", observation_block(regime_run.observation)))

    debate: tuple[DebateTurn, ...] = ()
    if mode.debate_rounds:
        debate = run_debate(
            ctx, symbol=symbol, evidence=evidence, analyst_blocks=blocks, rounds=mode.debate_rounds
        )
        for t in debate:
            journal.event(
                "AGENT_OBSERVATION",
                {
                    "trace_id": trace_id,
                    "round": t.round,
                    **t.run.observation.model_dump(mode="json"),
                },
            )
    transcript = [
        (f"debate/{t.side}/{t.round}", observation_block(t.run.observation))
        for t in debate
        if ok(t.run)
    ]

    critic = None
    if mode.critic:
        critic = run_agent(
            DEBATERS["critic"],
            ctx,
            symbol=symbol,
            evidence=evidence,
            context_blocks=[*blocks, *transcript],
        )
        journal.event(
            "AGENT_OBSERVATION",
            {"trace_id": trace_id, **critic.observation.model_dump(mode="json")},
        )

    synthesis = None
    if mode.synthesizer_tier is not None:
        critique = [("critique", observation_block(critic.observation))] if ok(critic) else []
        synthesis = run_agent(
            DEBATERS["synthesizer"],
            ctx,
            symbol=symbol,
            evidence=evidence,
            context_blocks=[*blocks, *transcript, *critique],
            tier=mode.synthesizer_tier,
        )
        journal.event(
            "AGENT_OBSERVATION",
            {"trace_id": trace_id, **synthesis.observation.model_dump(mode="json")},
        )
    return SymbolState(
        symbol, trace_id, tools, market_evidence, analysts, debate, critic, synthesis
    )


def _assert_pit(result: ToolResult, api: ResearchDataAPI) -> None:
    """Fail the symbol closed BEFORE any agent sees a fact from after the cutoff."""
    for e in result.evidence:
        if e.knowledge_ts > api.cutoff:
            raise LookaheadViolation(
                f"{e.evidence_id} knowledge_ts {e.knowledge_ts.isoformat()} is after "
                f"information_cutoff {api.cutoff.isoformat()}"
            )


def _status(runs: dict, agent: str) -> ComponentStatus:
    return runs[agent].observation.status if agent in runs else SKIPPED


def _build_signal(
    cfg: ResearchRunConfig,
    api: ResearchDataAPI,
    mode: ModeSpec,
    state: SymbolState,
    run_id: str,
    reading: RegimeReading,
    regime_obs: AgentObservation,
    cutoffs: dict[str, date | None],
    backend: str,
) -> ResearchSignal:
    """Assemble a ResearchSignal from observations. ai_score is derived from
    the analysts by code — the synthesizer contributes text and confidence
    only, so no single LLM call can set the directional score."""
    runs = state.analysts
    status = {a: _status(runs, a) for a in SYMBOL_ANALYSTS}
    status["regime"] = regime_obs.status
    status["debate"] = debate_status(state.debate)
    status["synthesis"] = state.synthesis.observation.status if state.synthesis else SKIPPED

    scores = {
        a: runs[a].observation.score if status[a] is ComponentStatus.OK else None
        for a in DIRECTIONAL
        if a in runs
    }
    by_id = {e.evidence_id: e for e in state.evidence()}

    synth = state.synthesis.output if ok(state.synthesis) else None
    if synth is not None:
        ai_conf = synth.ai_confidence
        supporting = synth.supporting_evidence_ids
        contradicting = synth.contradicting_evidence_ids
        bull, bear, consensus, risks = (
            synth.bull_case,
            synth.bear_case,
            synth.consensus,
            synth.risks,
        )
    else:
        confs = [
            runs[a].observation.confidence
            for a in DIRECTIONAL
            if a in runs and status[a] is ComponentStatus.OK
        ]
        ai_conf = sum(confs) / len(confs) if confs else None
        supporting = tuple(
            i
            for a in DIRECTIONAL
            if a in runs and status[a] is ComponentStatus.OK
            for i in runs[a].observation.evidence_ids
        )
        contradicting = ()
        last = {t.side: t.run.observation.summary for t in state.debate if ok(t.run)}
        bull, bear, consensus = last.get("bull", ""), last.get("bear", ""), ""
        risks = runs["risk"].observation.points if status["risk"] is ComponentStatus.OK else ()

    used_prompts = sorted(
        {r.observation.prompt_version for r in runs.values() if r.observation.prompt_version}
        | {
            t.run.observation.prompt_version
            for t in state.debate
            if t.run.observation.prompt_version
        }
        | {
            r.observation.prompt_version
            for r in (state.critic, state.synthesis)
            if r and r.observation.prompt_version
        }
        | ({regime_obs.prompt_version} if regime_obs.prompt_version else set())
    )
    sources = sorted({r.tool for results in state.tools.values() for r in results if r.ok})
    return ResearchSignal(
        research_id=run_id,
        trace_id=state.trace_id,
        symbol=state.symbol,
        market=api.market,
        timestamp=api.cutoff,
        information_cutoff=api.cutoff,
        research_mode=cfg.research_mode,
        market_regime=reading.label,
        regime_confidence=reading.confidence,
        technical_score=scores.get("technical"),
        fundamental_score=scores.get("fundamental"),
        news_score=scores.get("news"),
        sentiment_score=scores.get("sentiment"),
        risk_score=runs["risk"].observation.risk_score
        if status["risk"] is ComponentStatus.OK
        else None,
        component_status=status,
        ai_score=ai_score_of(scores, status),
        ai_confidence=ai_conf,
        bull_case=bull,
        bear_case=bear,
        consensus=consensus,
        supporting_evidence=tuple(by_id[i] for i in dict.fromkeys(supporting) if i in by_id),
        contradicting_evidence=tuple(by_id[i] for i in dict.fromkeys(contradicting) if i in by_id),
        risks=tuple(risks)[:6],
        data_sources=(*sources, f"tools:{TOOLS_VERSION}"),
        model_version=f"backend={backend};quick={cfg.quick_model.model_id};deep={cfg.deep_model.model_id}",
        prompt_version=",".join(used_prompts),
        knowledge_cutoffs=cutoffs,
        guard_days=cfg.guard_days,
        contamination_risk=is_contaminated(api.cutoff.date(), cutoffs, cfg.guard_days),
    )
