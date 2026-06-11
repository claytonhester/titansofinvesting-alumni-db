"""Diagnosis layer for the scorecard — 'what did well, what didn't, the lever'.

Two tiers:
  * deterministic (always on): a static cause->lever map. Each category, when it
    sits below its target, yields a specific finding (pulled from the run's
    sub-metrics) and a concrete next action grounded in this pipeline's real
    levers (the LinkedIn-first deep pass, profile_cleanup, the reconciler, the
    deep-path gate). Categories at/above target are reported as wins.
  * optional LLM narrative (scorecard.py --llm): a Sonnet pass reads the run +
    the worst sample profiles and writes a short 'top 3 fixes' note. Cost-gated,
    off by default. Lives here so the deterministic path has no LLM import.

The deterministic map is pure (run -> Diagnosis) and fully testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scorecard import ScorecardRun

# Same targets the table renders against — a category below its target is "weak".
from scorecard_render import TARGETS


@dataclass(frozen=True)
class DiagnosisItem:
    category: str
    finding: str   # what's wrong, with the number
    cause: str     # the most likely mechanism
    lever: str     # the concrete next action


@dataclass(frozen=True)
class Diagnosis:
    wins: tuple[str, ...] = field(default_factory=tuple)
    issues: tuple[DiagnosisItem, ...] = field(default_factory=tuple)


def _coverage_item(cat) -> DiagnosisItem:
    m = cat.metrics
    weakest = min(m.items(), key=lambda kv: kv[1]) if m else ("?", 0)
    return DiagnosisItem(
        "Coverage",
        f"{cat.score}/100; weakest field is {weakest[0]} at {weakest[1]}%",
        "Profiles enriched on a thin web footprint, or the LinkedIn spine "
        "wasn't reached for the gap fields.",
        "Run the LinkedIn-first deep pass on the low-coverage tail "
        "(phase2_enrich.py --policy deep --ids ...); check verifier reject-rate "
        "vs agent not-found to see which gate is dropping people.",
    )


def _coherence_item(cat) -> DiagnosisItem:
    by_rule = cat.metrics.get("by_rule", {})
    top = sorted(by_rule.items(), key=lambda kv: -kv[1])[:3]
    detail = ", ".join(f"{k}×{v}" for k, v in top) or "scattered"
    if cat.metrics.get("p0"):
        return DiagnosisItem(
            "Coherence",
            f"{cat.score}/100 with {cat.metrics['p0']} future-date P0(s)",
            "Impossible dates — a corrupted scrape or a namesake splice.",
            "STOP and inspect the P0 people before shipping; a future date "
            "means the wrong record merged in.",
        )
    lever = "Run profile_cleanup dedupe for zero-duration/duplicate roles; "
    if by_rule.get("has_dated_career"):
        lever += ("re-run the LinkedIn refresh (linkedin_refresh.py) on the "
                  "undated-career people to recover a dated spine; ")
    if by_rule.get("employer_in_history"):
        lever += ("for current!=latest-history, confirm the current employer is "
                  "captured as an open-ended career entry.")
    return DiagnosisItem(
        "Coherence", f"{cat.score}/100; failures: {detail}",
        "Reconcile/scrape artifacts — duplicate single-year roles, a current "
        "employer absent from the dated history, or fully undated careers.",
        lever.strip(),
    )


def _corroboration_item(cat) -> DiagnosisItem:
    m = cat.metrics
    return DiagnosisItem(
        "Corroboration",
        f"{cat.score}/100 ({m.get('corroborated', 0)}/{m.get('claims', 0)} "
        "claims confirmed by 2+ sources)",
        "Single-source claims dominate — sources aren't overlapping, or the "
        "reconciler isn't merging across them.",
        "Ensure Firecrawl+PDL+Perplexity all run before reconcile so the same "
        "fact gets a '+reconciled' multi-source tag; the LinkedIn-first run "
        "raises overlap by anchoring every source to one verified profile.",
    )


def _richness_item(cat) -> DiagnosisItem:
    m = cat.metrics
    return DiagnosisItem(
        "Richness",
        f"mean completeness {m.get('mean', cat.score)}; "
        f"{m.get('thin', 0)} thin profiles ({m.get('thin_pct', 0)}%)",
        "A long tail of sparse profiles drags the mean — usually the same "
        "people who fail Coverage.",
        "Target the thin tail with the deep pass; compute_completeness.py shows "
        "which component (role/edu/career/news/linkedin) is most often missing.",
    )


def _accuracy_item(cat) -> DiagnosisItem:
    return DiagnosisItem(
        "Accuracy",
        f"{cat.score}/100 vs gold ({cat.metrics.get('positives', 0)} positives)",
        "Extracted fields disagree with the hand-verified answer key — wrong "
        "employer/title/dates slipped through extraction or reconcile.",
        "Inspect the per-field misses in the gold report; tighten the "
        "extraction prompt or the reconcile tiebreak for the failing field.",
    )


def _cost_item(cat) -> DiagnosisItem:
    m = cat.metrics
    return DiagnosisItem(
        "Cost efficiency",
        f"{cat.score}/100 at ${m.get('usd_per_verified', '?')}/verified profile",
        "Spend per verified profile is above target — likely deep-path firing "
        "on low-signal people or PDL retries that don't land.",
        "Tighten the deep-path gate (force_deep_path) so the expensive LinkedIn "
        "agent only fires where there's a verifiable footprint; cap PDL retries.",
    )


_BUILDERS = {
    "coverage": _coverage_item,
    "coherence": _coherence_item,
    "corroboration": _corroboration_item,
    "richness": _richness_item,
    "accuracy": _accuracy_item,
    "cost": _cost_item,
}


def diagnose(run: "ScorecardRun") -> Diagnosis:
    """Deterministic cause->lever map. A measured category below its target
    becomes an issue; at/above target it's a win. Identity violations and
    regressions get their own explicit findings."""
    wins: list[str] = []
    issues: list[DiagnosisItem] = []

    for key, builder in _BUILDERS.items():
        cat = run.categories.get(key)
        if cat is None or cat.score is None:
            continue
        target = TARGETS.get(key, 100)
        if cat.score >= target:
            wins.append(f"{cat.name.title()} {cat.score} (≥{target})")
        else:
            issues.append(builder(cat))

    # Identity is special: a violation is the single most important finding.
    ident = run.categories.get("identity")
    if ident and ident.metrics.get("violations"):
        v = ident.metrics["violations"]
        issues.insert(0, DiagnosisItem(
            "Identity safety",
            f"{len(v)} gold violation(s): {v[0]}",
            "A ghost was filled or a must-reject URL leaked — a namesake/echo "
            "regression of the kind that caused the Ricardo Lopez P0.",
            "Do not ship. Re-run remediation on the named person and re-audit "
            "the identity gate before the next batch.",
        ))
    elif ident and ident.score is not None and ident.score >= TARGETS.get("identity", 75):
        wins.append(f"Identity {ident.score} (gold clean)")

    reg = run.categories.get("regression")
    if reg and reg.metrics.get("drop_count"):
        dropped = reg.metrics["dropped"]
        issues.append(DiagnosisItem(
            "Regression",
            f"{reg.metrics['drop_count']} people regressed vs prior "
            f"(ids: {dropped})",
            "A refresh may have overwritten good data, or a source went stale.",
            "Diff the named people's claims against the prior snapshot; the "
            "append-only refresh should never lower a completed profile.",
        ))

    return Diagnosis(tuple(wins), tuple(issues))


def render_diagnosis(diag: Diagnosis) -> str:
    """Markdown footer block for the terminal table."""
    lines: list[str] = []
    if diag.wins:
        lines.append("What went well:")
        for w in diag.wins:
            lines.append(f"  ✓ {w}")
    if diag.issues:
        lines.append("")
        lines.append("What to improve (cause → lever):")
        for it in diag.issues:
            lines.append(f"  ▸ {it.category}: {it.finding}")
            lines.append(f"      cause: {it.cause}")
            lines.append(f"      lever: {it.lever}")
    if not diag.wins and not diag.issues:
        lines.append("No measured categories to diagnose.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Optional LLM narrative (off by default; only imported when --llm is set)    #
# --------------------------------------------------------------------------- #

_NARRATIVE_PROMPT = """You are the quality lead for an alumni-intelligence \
enrichment pipeline. Below is a batch scorecard (JSON) and the deterministic \
diagnosis. Write a SHORT review (<200 words): one sentence on overall health, \
then the top 3 concrete fixes to make the NEXT batch better, ranked by impact. \
Be specific and reference the numbers. Do not invent metrics.

SCORECARD:
{scorecard_json}

DETERMINISTIC DIAGNOSIS:
{diagnosis_text}
"""


@dataclass(frozen=True)
class Narrative:
    text: str
    tokens_in: int = 0
    tokens_out: int = 0


def llm_narrative(run_json: dict, diag: Diagnosis, *, client, model: str) -> Narrative:
    """Optional Sonnet/Opus narrative. Caller owns the client + cost logging;
    this shapes the prompt, returns the text and token usage. Never raises — a
    model error returns an empty Narrative so the deterministic diagnosis stands."""
    import json as _json

    prompt = _NARRATIVE_PROMPT.format(
        scorecard_json=_json.dumps(run_json, indent=2)[:6000],
        diagnosis_text=render_diagnosis(diag)[:2000],
    )
    try:
        resp = client.messages.create(
            model=model, max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = "".join(
            b.text for b in resp.content if getattr(b, "type", "") == "text"
        ).strip()
        usage = getattr(resp, "usage", None)
        return Narrative(
            text,
            getattr(usage, "input_tokens", 0) or 0,
            getattr(usage, "output_tokens", 0) or 0,
        )
    except Exception:  # noqa: BLE001 — diagnosis must survive a model failure
        return Narrative("")
