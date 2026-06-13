"""Markdown research-report writer for the Alpha Engine (ADR-031).

Every report embeds its ``ResearchManifest`` summary — a report without a manifest
is invalid (reproducibility, ADR-031 #13).
"""

from __future__ import annotations

import pandas as pd

from alpha_engine.research.manifest import ResearchManifest


def _df_md(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return "_(no data)_\n"
    try:
        return df.to_markdown(index=False) + "\n"
    except Exception:
        # to_markdown needs `tabulate`; fall back to a fixed-width string.
        return "```\n" + df.to_string(index=False) + "\n```\n"


def render_report(
    *,
    title: str,
    manifest: ResearchManifest,
    sections: list[tuple[str, object]],
) -> str:
    """Render a markdown report. ``sections`` is a list of (heading, body) where
    body is a DataFrame, a dict, or a string."""
    parts: list[str] = [f"# {title}\n", "## Reproducibility manifest\n", manifest.summary_md(), "\n"]
    for heading, body in sections:
        parts.append(f"## {heading}\n")
        if isinstance(body, pd.DataFrame):
            parts.append(_df_md(body))
        elif isinstance(body, dict):
            for k, v in body.items():
                parts.append(f"- **{k}**: {v}\n")
        else:
            parts.append(f"{body}\n")
        parts.append("\n")
    return "".join(parts)
