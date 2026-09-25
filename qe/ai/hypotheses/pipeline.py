"""Hypothesis generation from a research run (ADR-043 P6).

The agent proposes; code annotates every draft with facts the model must not
be trusted to state:

  * ``eliminated_family`` — alias match against governance/research-eliminated-
    families.yaml (the consolidation memo's settled list) → flagged re-proposal;
  * ``testable_now``      — every required data kind is in the lake;
  * ``family_experiment_count`` — distinct experiments already registered in
    the family (governance/experiment-registry.jsonl) = the multiple-testing
    denominator a result in this family will carry.

Drafts are CANDIDATE-only research output under reports/qe-ai/hypotheses/.
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
import re
from typing import Annotated, Literal
import uuid

from pydantic import Field

from qe.ai.agents import AgentContext, run_agent
from qe.ai.agents.hypothesis import SPEC
from qe.ai.agents.prompts import GateItem
from qe.ai.config import ResearchRunConfig, load_yaml
from qe.ai.guardrails import redact
from qe.ai.llm import CircuitBreaker, LLMClient, LLMGateway, ResponseCache, RunBudget, build_client
from qe.ai.models import ComponentStatus, Evidence
from qe.ai.orchestration.journal import ResearchJournal
from qe.ai.paths import AI_REPORT_DIR, safe_write_path
from qe.ai.reporting import load_research_journal
from qe.clock import market_close_time, market_tz
from qe.config import FrozenModel
from qe.version import code_version

ELIMINATED_RELPATH = Path("governance") / "research-eliminated-families.yaml"
REGISTRY_RELPATH = Path("governance") / "experiment-registry.jsonl"
JOURNAL_MODE = "ai-hypotheses"
# What the lake actually holds (docs/architecture/current-state.md §4). Anything
# else (fundamentals, news, sentiment, intraday chains) is not testable today.
AVAILABLE_DATA = frozenset(
    {
        "nse_eod_prices",
        "nse_delivery_pct",
        "nse_turnover",
        "india_vix",
        "nifty_futures_eod",
        "nifty_options_eod",
        "us_eod_prices",
    }
)


class HypothesisDraft(FrozenModel):
    schema_version: Literal["hypothesis_draft/1"] = "hypothesis_draft/1"
    draft_id: str
    research_id: str
    name: str
    family: str
    hypothesis: Annotated[str, Field(max_length=600)]
    rationale: Annotated[str, Field(max_length=600)]
    required_data: tuple[str, ...]
    proposed_study_kind: str
    proposed_gates: tuple[GateItem, ...]
    # computed by code, never by the model
    testable_now: bool
    missing_data: tuple[str, ...]
    eliminated_family: str | None
    eliminated_status: str | None
    eliminated_reason: str | None
    family_experiment_count: int
    status: Literal["CANDIDATE"] = "CANDIDATE"
    requires_human_review: Literal[True] = True
    generated_by: str
    prompt_version: str
    prompt_hash: str


@dataclass(frozen=True)
class HypothesisRun:
    run_id: str
    journal_path: Path
    out_dir: Path | None
    drafts: tuple[HypothesisDraft, ...]
    status: ComponentStatus
    error: str | None = None


def load_eliminated_families(base_dir: str | Path) -> list[dict]:
    path = Path(base_dir) / ELIMINATED_RELPATH
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — hypothesis generation needs the settled list")
    return list(load_yaml(path)["families"])


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def match_eliminated(family: str, hypothesis: str, families: list[dict]) -> dict | None:
    """First settled family whose id or any alias appears in the draft's family
    slug or hypothesis text (normalized, whole-phrase match)."""
    haystack = f" {_norm(family)} {_norm(hypothesis)} "
    for fam in families:
        for phrase in (fam["id"], *fam.get("aliases", ())):
            if f" {_norm(phrase)} " in haystack:
                return fam
    return None


def family_budget(base_dir: str | Path) -> dict[str, int]:
    """Distinct experiments per family, read from the ledger file (qe.ai reads
    governance; it never imports qe.research or writes the ledger)."""
    path = Path(base_dir) / REGISTRY_RELPATH
    seen: dict[str, set[str]] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            if line.strip():
                rec = json.loads(line)
                seen.setdefault(rec.get("family", ""), set()).add(rec["experiment_id"])
    return {fam: len(ids) for fam, ids in seen.items()}


def _evidence(view, cutoff: datetime) -> tuple[Evidence, ...]:
    sigs = view.signals
    scored = [s.ai_score for s in sigs if s.ai_score is not None]
    facts = [
        ("n_signals", len(sigs), "research signals in the source run"),
        ("n_scored", len(scored), "signals with an AI score"),
        ("mean_ai_score", sum(scored) / len(scored) if scored else None, "mean AI score"),
        ("contaminated", sum(s.contamination_risk for s in sigs), "contaminated signals"),
        ("regime", sigs[0].market_regime if sigs else None, "market regime at the cutoff"),
    ]
    return tuple(
        Evidence(
            evidence_id=f"hyp.{name}",
            tool="hyp",
            symbol=None,
            knowledge_ts=cutoff,
            value=round(v, 6) if isinstance(v, float) else v,
            summary=summary,
        )
        for name, v, summary in facts
        if v is not None
    )


def generate_hypotheses(
    research_journal: str | Path,
    *,
    base_dir: str | Path = ".",
    client: LLMClient | None = None,
    allow_spend: bool = False,
) -> HypothesisRun:
    base_dir = Path(base_dir)
    view = load_research_journal(research_journal)
    cfg = ResearchRunConfig.model_validate(view.header["config"])
    families = load_eliminated_families(base_dir)
    budget = family_budget(base_dir)
    market = view.manifest.get("market", "NSE")
    as_of = date.fromisoformat(view.manifest["decision_date"])
    cutoff = datetime.combine(as_of, market_close_time(market), tzinfo=market_tz(market))
    client = client or build_client(cfg, allow_spend=allow_spend)
    research_id = view.header["session_id"]

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    run_id = f"hyp-{cfg.name}-{stamp}-{uuid.uuid4().hex[:6]}"
    with ResearchJournal(base_dir, run_id) as journal:
        journal.start(
            run_id=run_id,
            config=cfg,
            code_sha=code_version(base_dir),
            snapshot_id=view.header.get("data_snapshot_id") or "none",
            mode=JOURNAL_MODE,
        )
        journal.event(
            "HYPOTHESIS_MANIFEST", {"research_id": research_id, "as_of": as_of.isoformat()}
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
        blocks = [
            (
                "settled-families",
                [{"id": f["id"], "status": f["status"], "reason": f["reason"]} for f in families],
            ),
            ("family-test-budget", budget),
            ("available-data", sorted(AVAILABLE_DATA)),
            (
                "research-summary",
                [
                    {
                        "ai_score": s.ai_score,
                        "technical": s.technical_score,
                        "risk": s.risk_score,
                        "consensus": s.consensus,
                    }
                    for s in view.signals
                ],
            ),
        ]
        run = run_agent(
            SPEC, ctx, symbol=None, evidence=_evidence(view, cutoff), context_blocks=blocks
        )
        journal.event("AGENT_OBSERVATION", run.observation.model_dump(mode="json"))
        drafts: list[HypothesisDraft] = []
        if run.output is not None:
            for h in run.output.hypotheses:
                hit = match_eliminated(h.family, h.hypothesis, families)
                missing = tuple(d for d in h.required_data if d not in AVAILABLE_DATA)
                digest = hashlib.sha256(f"{h.family}\n{h.hypothesis}".encode()).hexdigest()
                drafts.append(
                    HypothesisDraft(
                        draft_id=f"hyp-{digest[:12]}",
                        research_id=research_id,
                        name=h.name,
                        family=h.family,
                        hypothesis=h.hypothesis,
                        rationale=h.rationale,
                        required_data=h.required_data,
                        proposed_study_kind=h.proposed_study_kind,
                        proposed_gates=h.proposed_gates,
                        testable_now=not missing,
                        missing_data=missing,
                        eliminated_family=hit["id"] if hit else None,
                        eliminated_status=hit["status"] if hit else None,
                        eliminated_reason=hit["reason"] if hit else None,
                        family_experiment_count=budget.get(h.family, 0),
                        generated_by=f"{client.name}:{cfg.deep_model.model_id}",
                        prompt_version=SPEC.prompt_version,
                        prompt_hash=SPEC.prompt_hash,
                    )
                )
        for d in drafts:
            journal.event("HYPOTHESIS_DRAFT", d.model_dump(mode="json"))
        status = run.observation.status
        journal.end(str(status), {"n_drafts": len(drafts), "error": run.observation.error})

    out_dir = None
    if drafts:
        md = safe_write_path(base_dir, AI_REPORT_DIR / "hypotheses" / research_id / "drafts.md")
        md.parent.mkdir(parents=True, exist_ok=True)
        jl = safe_write_path(base_dir, AI_REPORT_DIR / "hypotheses" / research_id / "drafts.jsonl")
        jl.write_text(
            "".join(json.dumps(d.model_dump(mode="json"), sort_keys=True) + "\n" for d in drafts)
        )
        md.write_text(render_drafts(drafts, research_id))
        out_dir = md.parent
    return HypothesisRun(
        run_id,
        journal.path,
        out_dir,
        tuple(drafts),
        status,
        redact(run.observation.error or "") or None,
    )


def render_drafts(drafts: list[HypothesisDraft], research_id: str) -> str:
    lines = [
        f"# Hypothesis drafts — from `{research_id}`",
        "",
        "> **CANDIDATE drafts for human review (ADR-043 P6).** AI proposes; it cannot register,",
        "> test, or promote. Flags below are computed by code, not claimed by the model.",
        "",
    ]
    for d in drafts:
        lines += [f"## `{d.draft_id}` — {d.name} (family `{d.family}`)", "", d.hypothesis, ""]
        if d.eliminated_family:
            lines.append(
                f"- ⚠️ **Re-proposal of settled family `{d.eliminated_family}` "
                f"({d.eliminated_status})** — {d.eliminated_reason}. Needs a genuinely new angle "
                "and fresh data (consolidation memo §7)."
            )
        if not d.testable_now:
            lines.append(f"- ⛔ **Not testable now** — no source for: {', '.join(d.missing_data)}")
        lines += [
            f"- Family test budget: {d.family_experiment_count} experiment(s) already registered "
            f"→ this would be #{d.family_experiment_count + 1}",
            f"- Rationale: {d.rationale}",
            f"- Proposed study: `{d.proposed_study_kind}`; pre-registered gates: "
            + ", ".join(f"`{g.metric} {g.op} {g.value}`" for g in d.proposed_gates),
            "- Next step (a human, only if worth testing): "
            f"`python -m qe lifecycle transition --strategy {d.name} --to CANDIDATE "
            f"--family {d.family} --hypothesis-ref qe.ai:{d.draft_id} --approved-by <you>`, "
            "then commit a study config whose `experiment:` block pre-registers the gates.",
            "",
        ]
    return "\n".join(lines) + "\n"
