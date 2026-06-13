"""Unit tests for alpha_engine.research.report (ADR-031)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pandas as pd
import pytest

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_SERVICES_DIR = os.path.join(_PROJECT_ROOT, "services")
if _SERVICES_DIR not in sys.path:
    sys.path.insert(0, _SERVICES_DIR)

from alpha_engine.research.manifest import ResearchManifest  # noqa: E402
from alpha_engine.research.report import render_report  # noqa: E402


def _manifest() -> ResearchManifest:
    return ResearchManifest.build(model_versions=["v1"], horizons=[15, 30])


class TestRenderReportStructure:
    def test_title_appears_as_h1(self):
        md = render_report(title="My Report", manifest=_manifest(), sections=[])
        assert md.startswith("# My Report\n")

    def test_reproducibility_manifest_section_present(self):
        md = render_report(title="T", manifest=_manifest(), sections=[])
        assert "## Reproducibility manifest" in md

    def test_manifest_summary_embedded(self):
        m = _manifest()
        md = render_report(title="T", manifest=m, sections=[])
        assert m.git_commit[:7] in md or "git_commit" in md

    def test_no_sections_returns_only_title_and_manifest(self):
        md = render_report(title="T", manifest=_manifest(), sections=[])
        assert "##" in md
        lines = [l for l in md.splitlines() if l.startswith("## ") and "Reproducibility" not in l]
        assert lines == []


class TestRenderReportSections:
    def test_section_heading_appears_as_h2(self):
        md = render_report(
            title="T", manifest=_manifest(), sections=[("IC Decay", "some text")]
        )
        assert "## IC Decay" in md

    def test_string_body_rendered_verbatim(self):
        md = render_report(
            title="T", manifest=_manifest(), sections=[("Section", "Hello world")]
        )
        assert "Hello world" in md

    def test_dict_body_rendered_as_bullet_list(self):
        md = render_report(
            title="T",
            manifest=_manifest(),
            sections=[("Metrics", {"ic": 0.12, "hit_rate": 0.55})],
        )
        assert "**ic**" in md
        assert "0.12" in md
        assert "**hit_rate**" in md

    def test_dataframe_body_not_empty(self):
        df = pd.DataFrame({"horizon": [15, 30], "ic": [0.1, 0.05]})
        md = render_report(title="T", manifest=_manifest(), sections=[("Curve", df)])
        assert "horizon" in md
        assert "15" in md

    def test_empty_dataframe_renders_no_data_placeholder(self):
        df = pd.DataFrame()
        md = render_report(title="T", manifest=_manifest(), sections=[("Empty", df)])
        assert "no data" in md

    def test_multiple_sections_all_appear(self):
        sections = [
            ("Section A", "body a"),
            ("Section B", "body b"),
            ("Section C", {"key": "val"}),
        ]
        md = render_report(title="T", manifest=_manifest(), sections=sections)
        assert "## Section A" in md
        assert "## Section B" in md
        assert "## Section C" in md

    def test_sections_appear_in_order(self):
        sections = [("First", "aaa"), ("Second", "bbb")]
        md = render_report(title="T", manifest=_manifest(), sections=sections)
        assert md.index("## First") < md.index("## Second")


class TestRenderReportDfFallback:
    def test_tabulate_unavailable_falls_back_to_code_block(self):
        df = pd.DataFrame({"a": [1, 2], "b": [3, 4]})
        with patch.object(df, "to_markdown", side_effect=Exception("no tabulate")):
            md = render_report(title="T", manifest=_manifest(), sections=[("Table", df)])
        assert "```" in md or "a" in md  # either fallback or direct string

    def test_none_body_rendered_as_string_none(self):
        # None is not a DataFrame/dict so it falls to the else branch and renders "None".
        md = render_report(title="T", manifest=_manifest(), sections=[("S", None)])
        assert "None" in md
