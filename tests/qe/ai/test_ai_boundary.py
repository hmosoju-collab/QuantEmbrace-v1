"""The AI/quant boundary (docs/architecture/ai-quant-boundary.md), enforced.

qe.ai is advisory by construction: it may import only an allowlist of qe
modules, no broker/network/exec machinery, and no trading module may import
it. Checks are static (AST over source) plus one fresh-interpreter runtime
check, because the pytest session has already imported qe.engine.
"""

import ast
from pathlib import Path
import re
import subprocess
import sys

import pytest

from qe.ai.paths import UnsafeWritePath, safe_write_path

REPO = Path(__file__).resolve().parents[3]
QE_AI = REPO / "qe" / "ai"

ALLOWED_QE = {
    "qe.config",
    "qe.journal",
    "qe.data.lake",
    "qe.data.panel",
    "qe.data.snapshot",
    "qe.data.feed",
    "qe.strategy.base",
    "qe.universe",
    "qe.clock",
    "qe.version",
}
BANNED_TOP = {
    # network / process / code loading
    "socket", "subprocess", "requests", "urllib", "urllib3", "httpx", "http", "aiohttp",
    "pickle", "joblib", "shelve", "marshal", "ctypes", "multiprocessing",
    # brokers, other LLM SDKs, agent frameworks
    "kiteconnect", "alpaca", "alpaca_trade_api", "anthropic", "openai",
    "langchain", "langchain_core", "langgraph",
    # v1 services (on pythonpath as top-level packages) — qe never imports them
    "services", "shared", "ai_engine", "alpha_engine", "backtesting", "data_ingestion",
    "execution_engine", "risk_engine", "strategy_engine", "monitoring_agent",
}  # fmt: skip
AWS_TOP = {"boto3", "botocore"}  # banned everywhere: the Bedrock adapter uses the Anthropic SDK
# The only modules that may import `anthropic`: one file per real backend. The shared
# Messages-API logic (llm/messages.py) deliberately imports no SDK.
SDK_ALLOWED_FILES = {QE_AI / "llm" / "bedrock.py", QE_AI / "llm" / "anthropic_api.py"}
BANNED_CALLS = {"eval", "exec", "compile", "__import__"}
BANNED_ATTR_CALLS = {("os", "system"), ("os", "popen"), ("os", "getenv"), ("os", "environ")}
TRADING_MODULES = [
    REPO / "qe" / "engine",
    REPO / "qe" / "execution.py",
    REPO / "qe" / "risk.py",
    REPO / "qe" / "portfolio.py",
    REPO / "qe" / "killswitch.py",
    REPO / "qe" / "live_gate.py",
    REPO / "qe" / "strategy",
    REPO / "qe" / "cli.py",
    REPO / "qe" / "research",
]
FORBIDDEN_API = re.compile(
    r"(place|submit|cancel|modify)_orders?|(set|update|close)_positions?|risk_limit"
    r"|promot|go_live|enable_live|(^|_)(de)?activate"
)


def _py_files(root: Path) -> list[Path]:
    return (
        [root]
        if root.is_file()
        else sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)
    )


def _imports(tree: ast.AST) -> list[str]:
    mods = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative imports are not used in qe"
            mods.append(node.module or "")
    return mods


AI_FILES = _py_files(QE_AI)


def test_qe_ai_package_exists():
    assert len(AI_FILES) >= 5


@pytest.mark.parametrize("path", AI_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_qe_ai_imports_only_the_allowlist(path):
    tree = ast.parse(path.read_text())
    for mod in _imports(tree):
        top = mod.split(".")[0]
        if mod.startswith("qe.ai") or mod == "qe.ai":
            continue
        if top == "qe":
            assert mod in ALLOWED_QE, f"{path.name}: forbidden qe import {mod}"
        if top == "anthropic":
            assert path in SDK_ALLOWED_FILES, f"{path.name}: anthropic only in the backend files"
        else:
            assert top not in BANNED_TOP, f"{path.name}: banned import {mod}"
        assert top not in AWS_TOP, f"{path.name}: boto3/botocore are not used by qe.ai"


@pytest.mark.parametrize("path", AI_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_qe_ai_has_no_exec_shell_or_env_access(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id not in BANNED_CALLS, f"{path.name}: {node.func.id}()"
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            assert (node.value.id, node.attr) not in BANNED_ATTR_CALLS, (
                f"{path.name}: {node.value.id}.{node.attr} (no env/shell access in qe.ai)"
            )


@pytest.mark.parametrize("path", AI_FILES, ids=lambda p: str(p.relative_to(REPO)))
def test_qe_ai_exposes_no_trading_or_promotion_api(path):
    tree = ast.parse(path.read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            assert not FORBIDDEN_API.search(node.name.lower()), f"{path.name}: {node.name}"


@pytest.mark.parametrize(
    "path",
    [f for root in TRADING_MODULES for f in _py_files(root)],
    ids=lambda p: str(p.relative_to(REPO)),
)
def test_no_trading_module_imports_qe_ai(path):
    for mod in _imports(ast.parse(path.read_text())):
        assert not mod.startswith("qe.ai"), f"{path} imports {mod}"


def test_fresh_interpreter_import_graph_is_clean():
    code = (
        "import importlib, pkgutil, sys\n"
        "import qe.ai\n"
        "for m in pkgutil.walk_packages(qe.ai.__path__, 'qe.ai.'):\n"
        "    if not m.name.endswith('__main__'):\n"
        "        importlib.import_module(m.name)\n"
        "bad = ('qe.execution', 'qe.engine', 'qe.risk', 'qe.portfolio', 'qe.killswitch',\n"
        "       'qe.live_gate', 'qe.research', 'qe.cli', 'kiteconnect', 'alpaca', 'boto3')\n"
        "print(','.join(n for n in bad if n in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], cwd=REPO, capture_output=True, text=True, timeout=120
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "", f"qe.ai import graph pulled in: {out.stdout.strip()}"


@pytest.mark.parametrize(
    "rel",
    [
        "journals/ai/run.jsonl",
        "reports/qe-ai/run/summary.md",
        "backtest-data/ai_cache/ab/k.json",
        "backtest-data/ai_corpus/curated/announcements.jsonl",
    ],
)
def test_safe_write_path_allows_ai_roots(tmp_path, rel):
    assert safe_write_path(tmp_path, rel) == (tmp_path / rel).resolve()


@pytest.mark.parametrize(
    "rel",
    [
        "reports/qe/delivery-book-x/summary.json",  # forward/live-gate evidence
        "journals/paper-delivery-book-paper-x.jsonl",  # live-gate clean-session evidence
        "journals/ai/paper-x.jsonl",  # paper-* name anywhere
        "journals/x.jsonl",
        "backtest-data/paper_book/qe_kill_switch.json",
        "backtest-data/raw/nse_announcements/ingest=x/announcements.jsonl",  # raw zone: scripts only
        "backtest-data/lake/ohlcv/market=NSE/segment=EQ/x.parquet",
        "governance/experiment-registry.jsonl",
        "governance/live-gate/operator-approval.json",
        "configs/qe_delivery_book_paper.yaml",
        "reports/qe-ai/../qe/x/summary.json",
        "../outside.txt",
        "journals/ai",  # the root itself is not a file target
    ],
)
def test_safe_write_path_refuses_everything_else(tmp_path, rel):
    with pytest.raises(UnsafeWritePath):
        safe_write_path(tmp_path, rel)


def test_safe_write_path_refuses_symlink_escape(tmp_path):
    (tmp_path / "reports" / "qe").mkdir(parents=True)
    (tmp_path / "journals").mkdir()
    (tmp_path / "journals" / "ai").symlink_to(tmp_path / "reports" / "qe")
    with pytest.raises(UnsafeWritePath):
        safe_write_path(tmp_path, "journals/ai/summary.json")
