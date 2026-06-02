"""The ONLY billed part of Phase 3 — two disciplined Haiku calls.

`insights_rollup.py` measures everything by SQL GROUP BY; this module is the
opt-in overlay the orchestrator runs when `--llm` is set. Two calls, both held
to the same evidence-bound discipline as `structuring.py`:

1. `classify_seniority` — maps each DISTINCT raw title onto the fixed
   SENIORITY_TIERS ladder. One call over the whole vocabulary (not one per
   person), temperature 0, JSON only. The model may ONLY pick a ladder label or
   "Unknown"; it never invents a tier. Falls back to the free keyword classifier
   for any title it skips or mislabels.

2. `write_narrative` — ONE call that writes the cohort prose over numbers that
   were ALREADY computed deterministically. It may rephrase and connect, but it
   must NEVER invent or alter a statistic; every figure it states is handed to it.

Both return token counts (from response.usage) so the orchestrator can log spend
via cost_log. Neither raises on a bad model response — a failed call degrades to
the deterministic fallback, never to a crash mid-run.
"""
from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from anthropic import Anthropic

from insights_rollup import classify_seniority_keyword
from insights_store import (
    SENIORITY_TIERS,
    SENIORITY_UNKNOWN,
    FirmCount,
    SeniorityTier,
)

# Haiku 4.5 — same cheap, disciplined tier the rest of the pipeline uses.
HAIKU_MODEL = "claude-haiku-4-5-20251001"

# Every label the classifier is allowed to emit. Anything outside this set is
# rejected in code and folded back to the keyword classifier — the model cannot
# widen the ladder by hallucinating a bucket.
_ALLOWED_TIERS = frozenset(SENIORITY_TIERS) | {SENIORITY_UNKNOWN}

_SENIORITY_SYSTEM = """You classify job titles onto a FIXED seniority ladder.

You MUST map every title to EXACTLY ONE of these labels, copied verbatim:
- "Analyst / Associate"
- "VP / Principal"
- "Director / Managing Director"
- "Partner / Founder"
- "C-suite / Owner"
- "Unknown"

Rules:
- Choose the label that matches the title's SENIORITY, not its function.
- "Vice President" / "VP" / "SVP" / "EVP" -> "VP / Principal" (never C-suite).
- "Managing Partner" / "General Partner" / "Founder" -> "Partner / Founder".
- "Managing Director" / "Executive Director" / "Head of" -> "Director / Managing Director".
- "Chief ___" / CEO / CFO / COO / President / Chairman / Owner -> "C-suite / Owner".
- "Analyst" / "Associate" / "Intern" / "Advisor" -> "Analyst / Associate".
- If a title is too vague to place, use "Unknown". Do NOT guess.
- Do NOT invent any label outside the six above.

Output ONLY a JSON object mapping each input title to its label, no prose."""

_NARRATIVE_SYSTEM = """You write a short, plain summary of an alumni cohort using \
ONLY the numbers you are given.

Rules:
- Every figure in your summary MUST come from the provided numbers. Do NOT \
invent, estimate, round differently, or infer any statistic.
- You may rephrase and connect the facts into readable prose. You may NOT add \
facts that aren't in the numbers (no industries, no names, no trends not shown).
- Write 2-4 sentences, third person, professional and plain. No editorializing \
("impressive", "elite"), no preamble, no labels, no markdown.
- Output ONLY the summary sentences."""


@dataclass(frozen=True)
class SeniorityClassification:
    """LLM seniority overlay: the folded ladder plus the token cost of producing
    it. `tiers` is already in the store's canonical order with Unknown last, so
    it can be dropped straight onto a snapshot via with_llm_narrative."""

    tiers: tuple[SeniorityTier, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class NarrativeResult:
    """The billed prose plus its token cost. `text` is empty when the call
    produced nothing usable, signalling the orchestrator to keep the template."""

    text: str
    input_tokens: int
    output_tokens: int


def _parse_json(text: str) -> dict:
    """Strip ```json fences and parse; never raise — return {} so a bad model
    response degrades to the deterministic fallback instead of killing the run."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return {}
        return {}


def classify_seniority(
    client: Anthropic,
    title_counts: Sequence[tuple[str, int]],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 2048,
    fallback: Callable[[str], str] = classify_seniority_keyword,
) -> SeniorityClassification:
    """Map distinct titles onto the ladder with ONE Haiku call, then fold the
    (title, count) pairs into per-tier totals. Any title the model omits or
    labels off-ladder falls back to the free keyword classifier, so coverage is
    always total and the result can never contain an invented tier. Returns the
    canonical-ordered ladder (Unknown last) plus token counts.

    With no titles or no client work to do this still returns a valid empty
    ladder at zero token cost."""
    distinct = sorted({title for title, _ in title_counts if title.strip()})
    if not distinct:
        return SeniorityClassification((), 0, 0)

    user = (
        "Classify each of these job titles onto the ladder. Return a JSON object "
        'mapping the EXACT title string to its label, e.g. {"Managing Director": '
        '"Director / Managing Director"}.\n\nTitles:\n'
        + "\n".join(f"- {t}" for t in distinct)
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {"type": "text", "text": _SENIORITY_SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    mapping = _parse_json(text)

    def label_for(title: str) -> str:
        proposed = mapping.get(title)
        if isinstance(proposed, str) and proposed in _ALLOWED_TIERS:
            return proposed
        return fallback(title)

    totals: dict[str, int] = {}
    for title, count in title_counts:
        if not title.strip():
            continue
        tier = label_for(title)
        totals[tier] = totals.get(tier, 0) + count

    ordered: list[SeniorityTier] = [
        SeniorityTier(tier=tier, count=totals[tier])
        for tier in SENIORITY_TIERS
        if totals.get(tier)
    ]
    if totals.get(SENIORITY_UNKNOWN):
        ordered.append(SeniorityTier(SENIORITY_UNKNOWN, totals[SENIORITY_UNKNOWN]))

    return SeniorityClassification(
        tiers=tuple(ordered),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def write_narrative(
    client: Anthropic,
    *,
    people: int,
    enriched: int,
    firms: Sequence[FirmCount],
    distinct_employers: int,
    founders_partners: int,
    seniority: Sequence[SeniorityTier] = (),
    model: str = HAIKU_MODEL,
    max_tokens: int = 320,
) -> NarrativeResult:
    """ONE Haiku call that writes cohort prose over the pre-computed numbers. The
    numbers are handed to the model as a fact sheet; the system prompt forbids
    inventing or altering any statistic. Returns empty text (orchestrator keeps
    the template) when nothing is enriched or the call yields nothing usable."""
    if enriched == 0:
        return NarrativeResult("", 0, 0)

    firm_lines = "\n".join(f"  - {f.company}: {f.count} alumni" for f in firms[:5])
    senior_lines = "\n".join(f"  - {s.tier}: {s.count}" for s in seniority)
    facts = (
        f"Total alumni in cohort: {people}\n"
        f"Alumni with verified profile data so far: {enriched}\n"
        f"Distinct current employers on record: {distinct_employers}\n"
        f"Alumni in partner/founder/C-suite tiers: {founders_partners}\n"
        f"Top landing firms:\n{firm_lines or '  (none)'}\n"
        f"Seniority breakdown:\n{senior_lines or '  (none)'}"
    )
    user = (
        "Write the cohort summary now, using ONLY these numbers:\n\n" + facts
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {"type": "text", "text": _NARRATIVE_SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()

    return NarrativeResult(
        text=text,
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )
