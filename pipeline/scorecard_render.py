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

# The FLOOR per category: the minimum acceptable bar, owner-set. The bar the
# table actually shows RATCHETS above this — it's max(floor, your best run so
# far) — so the standard rises as the pipeline improves and never drops below
# the floor. Identity and Coherence floor at 100: any namesake leak or impossible
# date is unacceptable, full stop (both also hard-gate the grade).
FLOORS = {
    "coverage": 80, "accuracy": 90, "identity": 100, "richness": 75,
    "coherence": 100, "corroboration": 30, "cost": 90,
}

# Batches contain different people, so one chunk's score isn't strictly
# comparable to another's. Absorb that composition noise: a category is only
# flagged as *below bar* when it falls more than this under the ratcheted target.
RATCHET_SLACK = 2


def best_prior(prior_runs: list[dict], key: str) -> int | None:
    """The highest score this category has reached in any prior run (the record
    to beat). None if it was never measured."""
    scores = [
        cat["score"]
        for run in prior_runs
        for cat in [(run.get("categories") or {}).get(key)]
        if cat and cat.get("score") is not None
    ]
    return max(scores) if scores else None


def effective_targets(prior_runs: list[dict]) -> dict[str, int]:
    """The live bar per category: max(floor, best-so-far). Ratchets up as runs
    set new records; never below the owner-set floor."""
    out = {}
    for key, floor in FLOORS.items():
        bp = best_prior(prior_runs, key)
        out[key] = max(floor, bp) if bp is not None else floor
    return out


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
    targets = effective_targets(prior_runs)
    header = ["Category"]
    for c in cols:
        header.append(_short_label(c))
    header.append("► this run")
    header.append("Target (best/floor)")

    lines = ["| " + " | ".join(header) + " |"]
    lines.append("|" + "|".join(["---"] * len(header)) + "|")

    caveats: list[str] = []
    new_best = False
    for key, label in ROW_ORDER:
        cat: CategoryScore = run.categories[key]
        row = [label]
        for c in cols:
            row.append(_prior_cell(c, key))
        marker = "*" if cat.caveat else ""
        # ★ when this run sets a new high-water mark for the category.
        bp = best_prior(prior_runs, key)
        star = ""
        if cat.score is not None and (bp is None or cat.score > bp) \
                and cat.score >= FLOORS.get(key, 0):
            star = " ★"
            new_best = True
        row.append(f"**{_cell(cat.score)}{marker}**"
                   f"{_delta(cat.score, prior_runs, key)}{star}")
        row.append(str(targets.get(key, "—")))
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
    if new_best:
        lines.append("★ = new best for that category (the bar ratcheted up)")

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
