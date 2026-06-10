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

from insights_rollup import classify_seniority_keyword, clean_title_basic
from insights_store import (
    SENIORITY_TIERS,
    SENIORITY_UNKNOWN,
    FirmCount,
    SeniorityTier,
    SignatureStat,
    TitleCount,
)
from sector_classify import SECTOR_NAMES, classify_sector

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

# Every sector the classifier may emit — fed from the shared taxonomy so the
# prompt can never drift from SECTOR_NAMES. Anything off-list is rejected in code.
_ALLOWED_SECTORS = frozenset(SECTOR_NAMES)
_SECTOR_BULLETS = "\n".join(f'- "{s}"' for s in SECTOR_NAMES)

_SECTOR_SYSTEM = f"""You classify a person's CURRENT sector from their employer, \
job title, and industry, onto a FIXED list.

You MUST pick EXACTLY ONE label per person, copied verbatim:
{_SECTOR_BULLETS}

Rules:
- Weigh the INDUSTRY most, then the EMPLOYER name, then the TITLE.
- Choose the sector the person actually works in, not a generic guess.
- A law firm / attorney -> "Law / Legal". A hospital, clinic, pharma, or \
biotech -> "Healthcare & Life Sciences". A software / IT / internet company -> \
"Technology". A real-estate, realty, or property firm -> "Real Estate". An \
insurer -> "Insurance". A university, college, or school -> "Education & \
Academia". A nonprofit, foundation, or government body -> "Government & Nonprofit".
- Keep the finance buckets precise: bulge-bracket / advisory bank -> "Investment \
Banking"; buyout / growth / credit fund -> "Private Equity & Credit"; hedge fund \
or asset manager -> "Hedge Funds & Asset Mgmt"; strategy/management consultancy \
-> "Consulting"; audit / accounting firm -> "Accounting & Audit"; oil, gas, \
power, mining, or infrastructure -> "Energy & Real Assets".
- Use "Other / Operating" ONLY when the person works in a general corporate or \
operating role that none of the named sectors fits. Do NOT guess a sector you \
can't support.
- Do NOT invent any label outside the list above.

Output ONLY a JSON object mapping each input NUMBER (as a string) to its label, \
no prose."""


_TITLE_SYSTEM = """You normalize job titles into clean, canonical labels for a \
directory chart, so near-duplicate titles GROUP together instead of each \
appearing as its own one-off row.

Rules:
- Output a clean, human-readable canonical title in Title Case.
- REMOVE the employer / company / product / team name and any trailing context \
after a dash, an "at", or a comma that names a place, department, product, or \
specialty. Examples: "Assistant Professor, Department of Pathology" -> \
"Assistant Professor"; "AI Governance and Agentic AI Sales Leader (Subject \
Matter Expert) - IBM UKI Data Platforms" -> "Sales Leader".
- KEEP seniority / rank words: Senior, Junior, Associate, Assistant, Vice, \
Managing, Executive, Chief, Lead, Head, Partner, Principal, Director, Founder, \
Analyst.
- COLLAPSE a role's function or practice qualifier into the base role so variants \
group together: "Associate Attorney" -> "Associate"; "Associate - Private Equity" \
-> "Associate"; "Appellate Partner" -> "Partner". But NEVER drop a seniority word \
(do not turn "Senior Associate" into "Associate").
- Reuse the EXACT SAME canonical label for titles that mean the same role, so \
they merge into one row.
- Do NOT invent a role the title doesn't state. If a title is already clean and \
canonical, return it unchanged.

Output ONLY a JSON object mapping each input title (verbatim) to its canonical \
title, no prose."""


_NARRATIVE_SYSTEM = """You write a short cohort summary for an alumni directory \
using ONLY the numbers you are given. The cards below this summary already show \
the raw KPIs, so your job is to FRAME them as a story, not restate the list.

Every figure you state MUST come from the provided numbers. Do NOT invent, \
estimate, round differently, or infer any statistic. You may name a few of the \
listed top firms; do NOT name any other firm, person, or industry not in the \
numbers.

Write 2-4 sentences, third person, professional and plain, in this order:
1. Open with the COVERAGE CAVEAT: state how many alumni have verified profiles \
out of the cohort total, with the percentage, and frame it as an early read \
(coverage is still growing).
2. Then tell the TRAJECTORY: these alumni began as analysts and associates and \
have been climbing. Lead with the share at Managing Director or above among the \
most senior class and, if given, the average years from graduation to that rank; \
then the count holding partner or founder titles. Use the "Founders & partners" \
KPI verbatim for that count and label it exactly "partner or founder" — do NOT \
merge it with the C-suite tier or relabel it.
3. You MAY close by naming two or three of the top landing firms.

Bold the KEY figures with markdown double-asterisks (e.g. **87 of 1,056**, \
**42%**, **8 years**, **11**). Bold the numbers, not whole sentences — three to \
five bolded figures total. No editorializing ("impressive", "elite"), no \
preamble, no labels, no headings.
Output ONLY the summary sentences."""


@dataclass(frozen=True)
class SeniorityClassification:
    """LLM seniority overlay: the folded ladder plus the token cost of producing
    it. `tiers` is already in the store's canonical order with Unknown last, so
    it can be dropped straight onto a snapshot via with_llm_narrative."""

    tiers: tuple[SeniorityTier, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class SectorClassification:
    """LLM sector overlay: labels aligned to the input order, plus token cost.
    Every label is guaranteed to be one of SECTOR_NAMES (off-list responses fall
    back to the deterministic classifier)."""

    labels: tuple[str, ...]
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class TitleCanonicalization:
    """LLM title overlay: the regrouped canonical titles (count desc) plus token
    cost. Near-duplicate raw titles are folded onto a shared canonical label, so
    the 'What they're doing now' card stops fragmenting one role across rows."""

    titles: tuple[TitleCount, ...]
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


def classify_sectors(
    client: Anthropic,
    items: Sequence[tuple[str, str, str]],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 2048,
    fallback: Callable[[str, str], str] = classify_sector,
) -> SectorClassification:
    """Classify the AMBIGUOUS remainder onto the fixed sector taxonomy with ONE
    Haiku call. `items` is a list of (employer, title, industry) tuples — pass
    only the people the deterministic classifier left in the catch-all (callers
    de-dup first to keep the call small). The model sees employer + title +
    industry (richer than the keyword classifier, which ignores title), but may
    ONLY emit a label from SECTOR_NAMES; anything else folds back to the
    deterministic `fallback(employer, industry)`. Never raises: a bad response
    degrades entirely to the fallback. Returns labels aligned to input order.

    With no items this returns an empty result at zero token cost."""
    if not items:
        return SectorClassification((), 0, 0)

    lines = "\n".join(
        f'{i}. employer="{c}" title="{t or "(unknown)"}" industry="{ind or "(unknown)"}"'
        for i, (c, t, ind) in enumerate(items)
    )
    user = (
        "Classify each numbered person into ONE sector. Return a JSON object "
        'mapping the number (as a string) to the label, e.g. {"0": "Law / Legal"}.'
        "\n\n" + lines
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {"type": "text", "text": _SECTOR_SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    mapping = _parse_json(text)

    labels: list[str] = []
    for i, (company, _title, industry) in enumerate(items):
        proposed = mapping.get(str(i))
        if isinstance(proposed, str) and proposed in _ALLOWED_SECTORS:
            labels.append(proposed)
        else:
            labels.append(fallback(company, industry))

    return SectorClassification(
        labels=tuple(labels),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def canonicalize_titles(
    client: Anthropic,
    title_counts: Sequence[tuple[str, int]],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 3072,
    fallback: Callable[[str], str] = clean_title_basic,
) -> TitleCanonicalization:
    """Fold the raw current-title vocabulary onto clean canonical labels with ONE
    Haiku call, then regroup the (title, count) pairs by canonical label so
    near-duplicates merge into a single row (count summed). Any title the model
    omits or returns blank falls back to the free deterministic `clean_title_basic`
    tidy-up, so coverage is always total. Never raises: a bad response degrades to
    the deterministic cleaner for every title. Returns canonical titles ordered by
    count (desc), then alphabetically.

    With no titles this returns an empty result at zero token cost."""
    distinct = [(t, n) for t, n in title_counts if t and t.strip()]
    if not distinct:
        return TitleCanonicalization((), 0, 0)

    user = (
        "Normalize each of these job titles. Return a JSON object mapping the "
        'EXACT input title to its canonical title, e.g. {"Associate Attorney": '
        '"Associate"}.\n\nTitles:\n'
        + "\n".join(f"- {t}" for t, _ in distinct)
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[
            {"type": "text", "text": _TITLE_SYSTEM, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    mapping = _parse_json(text)

    def canon(title: str) -> str:
        proposed = mapping.get(title)
        if isinstance(proposed, str) and proposed.strip():
            return proposed.strip()
        return fallback(title) or title.strip()

    totals: dict[str, int] = {}
    for title, count in distinct:
        label = canon(title)
        totals[label] = totals.get(label, 0) + count

    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return TitleCanonicalization(
        titles=tuple(TitleCount(title=t, count=n) for t, n in ordered),
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
    kpis: Sequence[SignatureStat] = (),
    model: str = HAIKU_MODEL,
    max_tokens: int = 320,
) -> NarrativeResult:
    """ONE Haiku call that writes cohort prose over the pre-computed numbers. The
    numbers are handed to the model as a fact sheet; the system prompt forbids
    inventing or altering any statistic. The four headline KPIs (buy-side, MD+,
    founders, first-firm) are included when supplied so the prose can lead with
    them. Returns empty text (orchestrator keeps the template) when nothing is
    enriched or the call yields nothing usable."""
    if enriched == 0:
        return NarrativeResult("", 0, 0)

    firm_lines = "\n".join(f"  - {f.company}: {f.count} alumni" for f in firms[:5])
    senior_lines = "\n".join(f"  - {s.tier}: {s.count}" for s in seniority)
    kpi_lines = "\n".join(f"  - {k.label}: {k.value} ({k.detail})" for k in kpis)
    facts = (
        f"Total alumni in cohort: {people}\n"
        f"Alumni with verified profile data so far: {enriched}\n"
        f"Distinct current employers on record: {distinct_employers}\n"
        f"Alumni in partner/founder/C-suite tiers: {founders_partners}\n"
        f"Headline KPIs:\n{kpi_lines or '  (none)'}\n"
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
