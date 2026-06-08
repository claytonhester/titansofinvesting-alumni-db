"""Phase 2 structuring: ONE Haiku call turns concatenated source markdown into
confidence-scored fields with verbatim quotes. This is the traceability gate.

Discipline borrowed verbatim-in-spirit from fire-enrich's extraction prompt:
- extract ONLY what is explicitly stated; never use general knowledge
- per-field confidence 0-1 + the exact source URL + a verbatim quote
- null when not stated; drop anything with confidence <= 0.3
The model is a reasoning layer over evidence, NEVER a source of truth.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from anthropic import Anthropic

from discovery import Source

logger = logging.getLogger(__name__)

# Haiku 4.5 — cheapest tier that can do disciplined extraction well.
HAIKU_MODEL = "claude-haiku-4-5-20251001"
_MAX_SOURCE_CHARS = 12_000  # cap per source so one long page can't blow the context
_CONFIDENCE_FLOOR = 0.3

# A short_bio composed from already-verified claims rather than quoted from one
# source. Tagged distinctly so a synthesized bio is never mistaken for a direct
# extraction — its provenance is the structured claims it was built from.
BIO_SYNTHESIS_METHOD = "claude-haiku-4-5-synthesis"
_MIN_FACTS_FOR_BIO = 2  # below this there isn't enough to say to bother

_SYSTEM = """You are an expert data extractor building a professional profile.
You MUST ONLY extract information that is EXPLICITLY STATED in the provided \
sources. DO NOT make up, guess, infer, or use any general knowledge you may \
have about this person. If a field is not explicitly stated in the sources, \
return null for it.

For every non-null field you MUST provide:
- "value": the extracted information
- "confidence": a number 0.0-1.0 for how certain the sources make this
- "source_url": the exact URL of the source that states it
- "quote": a short verbatim quote from that source that supports the value

If multiple sources agree, raise confidence and prefer the most authoritative \
source for the quote. If sources conflict, pick the most authoritative and \
lower confidence. Output ONLY valid JSON, no prose."""

_SCHEMA_INSTRUCTION = """Return a JSON object with exactly these keys (use null \
for any field not explicitly supported by the sources):

{
  "current_title": {field} | null,
  "current_employer": {field} | null,
  "career_history": [ {field}, ... ],   // each prior role as a field object
  "education": [ {field}, ... ],
  "location": {field} | null,
  "public_links": [ {field}, ... ],     // articles, talks, profiles authored BY them
  "short_bio": {field} | null
}

where {field} = {"value": <string>, "confidence": <0.0-1.0>, \
"source_url": <string>, "quote": <string>}."""

_BIO_SYSTEM = """You write a one- or two-sentence professional bio from a fixed \
list of verified facts about a person.

Rules:
- Use ONLY the facts provided. Do NOT add, infer, embellish, or use any outside \
knowledge. Every claim in the bio must trace to a listed fact.
- Write 1-2 sentences, third person, professional and plain. Do not editorialize \
("accomplished", "renowned", etc.).
- It is fine to omit facts that don't fit naturally. Never invent connective \
facts (dates, relationships, reasons) that aren't given.
- Output ONLY the bio sentence(s). No preamble, no labels, no quotation marks."""


@dataclass(frozen=True)
class StructuringResult:
    full_name: str
    profile: dict
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class BioSynthesis:
    """A short_bio composed from verified facts. confidence is inherited from
    the weakest supporting claim — the bio is only as sure as its shakiest fact."""

    value: str
    confidence: float
    input_tokens: int
    output_tokens: int


def _concat_sources(sources: tuple[Source, ...]) -> str:
    blocks = []
    for i, s in enumerate(sources, 1):
        body = s.markdown[:_MAX_SOURCE_CHARS]
        blocks.append(f"<source id={i} url=\"{s.url}\" title=\"{s.title}\">\n{body}\n</source>")
    return "\n\n".join(blocks)


def _drop_low_confidence(node: object) -> object:
    """Recursively null/strip any field whose confidence <= floor. Keeps the
    never-trust-weak-evidence rule out of the prompt's good graces and in code."""
    if isinstance(node, dict):
        if "confidence" in node and "value" in node:
            try:
                if float(node.get("confidence", 0)) <= _CONFIDENCE_FLOOR:
                    return None
            except (TypeError, ValueError):
                return None
            return node
        return {k: _drop_low_confidence(v) for k, v in node.items()}
    if isinstance(node, list):
        cleaned = [_drop_low_confidence(x) for x in node]
        return [x for x in cleaned if x is not None]
    return node


def structure_profile(
    client: Anthropic,
    full_name: str,
    sources: tuple[Source, ...],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 2048,
) -> StructuringResult:
    """One extraction call over ALL sources at once (enables cross-source
    consensus). Returns parsed profile + token counts for cost measurement."""
    if not sources:
        return StructuringResult(full_name, {}, 0, 0)

    user = (
        f"Person to profile: {full_name}\n\n"
        f"{_SCHEMA_INSTRUCTION}\n\n"
        f"Sources follow. Extract ONLY what they explicitly state.\n\n"
        f"{_concat_sources(sources)}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        # Cache the static extraction instructions (0.1x input on cache hits); the
        # prefix is identical for every person so each run reuses it after call one.
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )

    text = "".join(block.text for block in response.content if block.type == "text")
    profile = _parse_json(text)
    profile = _drop_low_confidence(profile)

    return StructuringResult(
        full_name=full_name,
        profile=profile if isinstance(profile, dict) else {},
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _facts_from_profile(profile: dict) -> list[tuple[str, float]]:
    """Pull (value, confidence) pairs from a structured profile in a natural bio
    order. Each pair is one already-source-attributed claim, so a bio built from
    these stays evidence-bound."""
    facts: list[tuple[str, float]] = []

    def take(node: object) -> None:
        if isinstance(node, dict) and node.get("value"):
            try:
                conf = float(node.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            facts.append((str(node["value"]), conf))

    take(profile.get("current_title"))
    take(profile.get("current_employer"))
    for entry in profile.get("career_history") or []:
        take(entry)
    for entry in profile.get("education") or []:
        take(entry)
    take(profile.get("location"))
    return facts


def profile_from_claims(claims: list) -> dict:
    """Build the minimal profile-shaped dict synthesize_bio reads, from a flat
    ClaimRow list. Lets the bio be composed from ALL résumé sources (PDL included),
    not just Firecrawl's extraction — so a PDL-matched person with no scraped pages
    still gets a narrative. Public links / mentions are ignored (not bio facts)."""
    singles = {"current_title", "current_employer", "location", "short_bio"}
    lists = {"career_history", "education"}
    prof: dict = {}
    for c in claims:
        node = {"value": c.value, "confidence": c.confidence}
        if c.claim_type in singles:
            prof.setdefault(c.claim_type, node)  # first seen wins
        elif c.claim_type in lists:
            prof.setdefault(c.claim_type, []).append(node)
    return prof


def synthesize_bio(
    client: Anthropic,
    full_name: str,
    profile: dict,
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 256,
) -> BioSynthesis | None:
    """Compose a short_bio from already-verified claims when extraction found no
    ready-made narrative. Composes ONLY from the structured facts (never new
    knowledge), so the result stays as traceable as its inputs. Returns None when
    a real bio already exists or there aren't enough facts to be worth it."""
    existing = profile.get("short_bio")
    if isinstance(existing, dict) and existing.get("value"):
        return None

    facts = _facts_from_profile(profile)
    if len(facts) < _MIN_FACTS_FOR_BIO:
        return None

    fact_lines = "\n".join(f"- {value}" for value, _ in facts)
    user = (
        f"Person: {full_name}\n\n"
        f"Verified facts:\n{fact_lines}\n\n"
        "Write the bio now."
    )
    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": _BIO_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in response.content if b.type == "text").strip()
    if not text:
        return None

    return BioSynthesis(
        value=text,
        confidence=min(conf for _, conf in facts),
        input_tokens=response.usage.input_tokens,
        output_tokens=response.usage.output_tokens,
    )


def _parse_json(text: str) -> dict:
    """Models sometimes wrap JSON in ```json fences. Strip and parse; never raise
    on a bad response — return {} so one person can't kill a batch run."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[: -3]
    cleaned = cleaned.strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                logger.warning(
                    "structuring: brace-substring JSON parse failed; returning {} "
                    "(extraction lost for this record); head=%r",
                    cleaned[:120],
                )
                return {}
        logger.warning(
            "structuring: no JSON object found in model output; returning {} "
            "(extraction lost for this record); head=%r",
            cleaned[:120],
        )
        return {}
