"""P8 dashboard: every §24 field present, AI vs QuantEmbrace side by side,
untrusted LLM text escaped, no script or network surface."""

from datetime import date
import json
import re

import yaml

from qe.ai.cli import main
from qe.ai.config import ModelProfile, ResearchRunConfig
from qe.ai.dashboard import CSP, build_dashboard
from qe.ai.fusion import FusionConfig, run_fusion
from qe.ai.llm import FakeLLM
from qe.ai.llm.fake import default_payload
from qe.ai.orchestration import run_research
from qe.ai.post_trade import run_post_trade
from qe.ai.shadow import run_shadow
from qe.ai.tools import ResearchData
from qe.config import DataConfig, RunConfig, StrategyConfig, UniverseConfig
from qe.engine import run_sim
from qe.research import lifecycle

EVIL = '<script>alert(1)</script><img src=x onerror="alert(2)">'
SECTIONS = [
    "Market regime", "Quant score", "AI research score", "Risk score", "Bull:", "Bear:",
    "Consensus:", "Conflicting evidence:", "Strategy signal", "QuantEmbrace final decision",
    "AI recommendation", "Forward AI shadow gate", "Post-trade reviews", "Strategy lifecycle",
]  # fmt: skip


def _hostile(req):
    p = default_payload(req)
    if "bull_case" in p:
        p["bull_case"] = f"Strong setup {EVIL}"
    return json.dumps(p)


def test_empty_workspace_renders_placeholders_safely(tmp_path):
    html = build_dashboard(tmp_path).read_text()
    assert "No research run yet" in html and CSP in html
    assert "<script" not in html.lower()


def test_full_dashboard_escapes_untrusted_text(tmp_path, synthetic_panel):
    book = RunConfig(
        name="dash-book",
        mode="sim",
        start_date=date(2024, 9, 30),
        end_date=synthetic_panel.date_at(len(synthetic_panel.index) - 1),
        universe=UniverseConfig(symbols=None),
        data=DataConfig(lake_root="unused"),
        journal_dir=str(tmp_path / "journals"),
        strategy=StrategyConfig(factor="delivery", top_n=40, k=10),
    )
    data = ResearchData(synthetic_panel, "ds-test", {})
    cfg = ResearchRunConfig(
        name="dash",
        book_config="configs/book.yaml",
        research_mode="STANDARD",
        quick_model=ModelProfile(model_id="fake-quick", tier="quick"),
        deep_model=ModelProfile(model_id="fake-deep", tier="deep"),
        max_symbols=4,
    )
    (tmp_path / "configs").mkdir()
    (tmp_path / "configs" / "book.yaml").write_text(yaml.safe_dump(book.model_dump(mode="json")))
    (tmp_path / "configs" / "ai.yaml").write_text(yaml.safe_dump(cfg.model_dump(mode="json")))
    (tmp_path / "configs" / "gate.yaml").write_text(
        yaml.safe_dump({"research_config": "configs/ai.yaml"})
    )

    sim = run_sim(book, base_dir=tmp_path, panel=synthetic_panel)
    res = run_research(cfg, as_of=synthetic_panel.date_at(400), base_dir=tmp_path, book=book,
                       data=data, client=FakeLLM(_hostile))  # fmt: skip
    run_fusion(res.journal_path, FusionConfig(), base_dir=tmp_path, book=book, data=data)
    run_post_trade(
        sim.journal_path, cfg, base_dir=tmp_path, client=FakeLLM(), data=data, max_trades=5
    )
    run_shadow(
        "configs/gate.yaml", as_of=synthetic_panel.date_at(400), base_dir=tmp_path, book=book
    )
    lifecycle.transition(tmp_path, strategy_id="delivery-book", to_state="CANDIDATE",
                         approved_by="Hari", family="delivery-factor", hypothesis_ref="doc.md")  # fmt: skip

    assert main(["dashboard", "--base-dir", str(tmp_path)]) == 0
    html = (tmp_path / "reports" / "qe-ai" / "dashboard" / "index.html").read_text()
    for section in SECTIONS:
        assert section in html, section
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html  # shown as text...
    assert not re.search(r"<\s*script", html, re.I)  # ...never as markup
    assert not re.search(r"<\s*img", html, re.I)
    assert "http://" not in html and "https://" not in html  # no external loads
    assert f'content="{CSP}"' in html and "default-src 'none'" in html
    assert "SELECT" in html and "delivery-book" in html and "NOT_EVALUABLE" in html
