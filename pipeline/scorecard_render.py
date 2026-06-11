"""Markdown rendering for the batch scorecard — the model-card table.

Rows are categories, columns are the last N saved runs plus the run just scored
(highlighted with a ► marker) plus a Target column. Unmeasured cells render as
"—". A caveat on a category adds a "*" and a footnote line. The diagnosis footer
is filled in by Phase C; here it just lists the caveats verbatim.

Pure string assembly — no I/O, no DB — so it's trivially testable.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scorecard import CategoryScore, ScorecardRun

# Display order + friendly labels. Regression sits below the composite line.
ROW_ORDER = [
    ("coverage", "Coverage"),
    ("accuracy", "Accuracy"),
    ("identity", "Identity safety"),
    ("richness", "Richness"),
    ("coherence", "Coherence"),
    ("corroboration", "Corroboration"),
    ("cost", "Cost efficiency"),
]

# A loose, human "good enough" target per category for the Target column.
TARGETS = {
    "coverage": 80, "accuracy": 90, "identity": 75, "richness": 75,
    "coherence": 100, "corroboration": 30, "cost": 90,
}


def _cell(score: int | None) -> str:
    return "—" if score is None else str(score)


def _prior_cell(run: dict, key: str) -> str:
    cat = (run.get("categories") or {}).get(key)
    if not cat or cat.get("score") is None:
        return "—"
    return str(cat["score"])


def _delta(current: int | None, prior_runs: list[dict], key: str) -> str:
    """Signed delta vs the most recent prior run that measured this category."""
    if current is None:
        return ""
    for run in reversed(prior_runs):
        cat = (run.get("categories") or {}).get(key)
        if cat and cat.get("score") is not None:
            d = current - cat["score"]
            return f" ({d:+d})" if d else " (=)"
    return ""


def render_table(prior_runs: list[dict], run: "ScorecardRun", *,
                 history: int = 4) -> str:
    cols = prior_runs[-history:] if history else []
    header = ["Category"]
    for c in cols:
        header.append(_short_label(c))
    header.append("► this run")
    header.append("Target")

    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    caveats: list[str] = []
    for key, label in ROW_ORDER:
        cat: CategoryScore = run.categories[key]
        row = [label]
        for c in cols:
            row.append(_prior_cell(c, key))
        marker = "*" if cat.caveat else ""
        row.append(f"**{_cell(cat.score)}{marker}**{_delta(cat.score, prior_runs, key)}")
        row.append(str(TARGETS.get(key, "—")))
        lines.append("| " + " | ".join(row) + " |")
        if cat.caveat:
            caveats.append(f"{label}: {cat.caveat}")

    # Composite + regression summary lines.
    lines.append("")
    lines.append(f"**Composite: {run.composite}  ·  Grade: {run.grade}**"
                 f"{'  ·  ⚠ HARD GATE TRIPPED' if run.gated else ''}")
    reg = run.categories.get("regression")
    if reg and reg.score is not None:
        dropped = reg.metrics.get("drop_count", 0)
        lines.append(f"Regression vs prior: {reg.score}/100"
                     f" ({dropped} regressed of {reg.metrics.get('compared', 0)})")
    lines.append(f"Batch: {run.label}  ·  n={run.n}")

    if caveats:
        lines.append("")
        lines.append("Caveats:")
        for c in caveats:
            lines.append(f"  * {c}")
    return "\n".join(lines)


def _short_label(run: dict) -> str:
    """A compact column header for a prior run: its grade + date."""
    ts = (run.get("timestamp") or "")[:10]
    return f"{run.get('grade', '?')} {ts}"
