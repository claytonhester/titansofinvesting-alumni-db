"""Phase 2 identity resolution: the merge gate.

Discovery finds pages that MENTION a name. Many will be the wrong person
(common names, namesakes, unrelated companies). Before we trust any claim, one
Sonnet call judges — per source — whether it actually refers to OUR directory
alumnus, anchored on the only ground truth we have: the directory's
name + company + school + city + cohort.

Sonnet is a reasoning layer over that evidence, never a source of truth: it may
only use what the source text states plus the directory anchors. Its output is
a confidence per source, which the merge gate turns into a decision:

    confidence >= AUTO_ACCEPT   -> auto_accept   (claims may be trusted)
    LOW <= confidence < ACCEPT  -> review         (human approves/rejects)
    confidence <  LOW           -> reject          (dropped, but recorded)

EVERY verdict is returned (and persisted by the caller) — including rejects —
so a wrong merge is auditable and recoverable. We never silently drop a
candidate, and we never auto-merge an uncertain identity.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from discovery import Source
from enrichment_store import DECISION_ACCEPT, DECISION_REJECT, DECISION_REVIEW

# Sonnet 4.6 — the reasoning tier for disambiguation; a wrong merge is the
# project's biggest liability, so we do not economise here with Haiku.
SONNET_MODEL = "claude-sonnet-4-6"

# Merge-gate thresholds. AUTO_ACCEPT mirrors the plan's ~0.85 gate; anything
# from REVIEW_FLOOR up but below it goes to a human; below REVIEW_FLOOR is a
# confident non-match and is rejected (still recorded with its reason).
AUTO_ACCEPT = 0.85
REVIEW_FLOOR = 0.4

_MAX_SNIPPET_CHARS = 2_000  # identity only needs the top of the page, not all of it


@dataclass(frozen=True)
class PersonAnchors:
    """The directory ground truth we disambiguate against. cohort/school come
    from the section heading; company/city from the row."""

    full_name: str
    company: str
    city: str
    school: str
    titan_class: int


@dataclass(frozen=True)
class IdentityVerdict:
    """One source's merge-gate outcome. reason is Sonnet's short justification,
    kept verbatim for the audit trail."""

    source_url: str
    confidence: float
    decision: str
    reason: str


@dataclass(frozen=True)
class IdentityResult:
    """The full merge-gate outcome plus the Sonnet token cost of producing it,
    so the caller can account for identity-resolution spend (the Sonnet call the
    older Haiku-only cost model omitted)."""

    verdicts: tuple[IdentityVerdict, ...]
    input_tokens: int
    output_tokens: int


_SYSTEM = """You are an identity disambiguation expert building a professional \
profile database. You are given a KNOWN person (from an authoritative alumni \
directory) and a set of web sources that merely MENTION their name. Your job is \
to decide, for EACH source, how confident you are that it refers to the SAME \
person as the directory entry — not a namesake or unrelated individual.

Anchor your judgement ONLY on evidence: the directory facts (name, company, \
city, school, class year) and what each source actually states. Do NOT use \
outside knowledge about any real person. A matching employer, location, school, \
or career detail raises confidence; a clear conflict (different industry, \
different city with no link, wrong employer) lowers it. Absence of corroborating \
detail means LOW confidence, not high.

For each source output:
- "source_url": the exact url given
- "confidence": 0.0-1.0 that this source is the SAME person
- "reason": one short sentence citing the specific evidence you used

Output ONLY a JSON array, one object per source, no prose."""


def _anchor_block(anchors: PersonAnchors) -> str:
    return (
        "KNOWN PERSON (directory ground truth):\n"
        f"- name: {anchors.full_name}\n"
        f"- company (at time of directory): {anchors.company}\n"
        f"- city: {anchors.city}\n"
        f"- school: {anchors.school}\n"
        f"- Titan class: {anchors.titan_class}"
    )


def _sources_block(sources: tuple[Source, ...]) -> str:
    blocks = []
    for i, s in enumerate(sources, 1):
        snippet = s.markdown[:_MAX_SNIPPET_CHARS]
        blocks.append(
            f"<source id={i} url=\"{s.url}\" title=\"{s.title}\">\n"
            f"{snippet}\n</source>"
        )
    return "\n\n".join(blocks)


def _decide(confidence: float) -> str:
    if confidence >= AUTO_ACCEPT:
        return DECISION_ACCEPT
    if confidence >= REVIEW_FLOOR:
        return DECISION_REVIEW
    return DECISION_REJECT


def resolve_identity(
    client: Anthropic,
    anchors: PersonAnchors,
    sources: tuple[Source, ...],
    *,
    model: str = SONNET_MODEL,
    max_tokens: int = 1536,
) -> IdentityResult:
    """One Sonnet call scoring every source against the directory anchors.
    Returns a verdict per source (accept/review/reject) with its reason, plus the
    Sonnet token usage. Sources Sonnet omits from its answer default to a rejected
    verdict so no candidate is ever silently lost."""
    if not sources:
        return IdentityResult(verdicts=(), input_tokens=0, output_tokens=0)

    user = (
        f"{_anchor_block(anchors)}\n\n"
        f"Score each of the following {len(sources)} sources.\n\n"
        f"{_sources_block(sources)}"
    )

    response = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        # Cache the large static instruction prefix: cache reads bill at 0.1x
        # input. The prefix is identical across every person, so after the first
        # call each run reuses it. Stacks with the Batch API discount later.
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    text = "".join(b.text for b in response.content if b.type == "text")
    scored = _parse_scores(text)
    usage = response.usage

    return IdentityResult(
        verdicts=tuple(_verdict_for(source, scored) for source in sources),
        input_tokens=getattr(usage, "input_tokens", 0) or 0,
        output_tokens=getattr(usage, "output_tokens", 0) or 0,
    )


def accepted_sources(
    sources: tuple[Source, ...], verdicts: tuple[IdentityVerdict, ...]
) -> tuple[Source, ...]:
    """The subset safe to extract claims from: only auto-accepted identities.
    Review/reject sources are held back from structuring until a human acts."""
    accepted_urls = {
        v.source_url for v in verdicts if v.decision == DECISION_ACCEPT
    }
    return tuple(s for s in sources if s.url in accepted_urls)


def _verdict_for(
    source: Source, scored: dict[str, tuple[float, str]]
) -> IdentityVerdict:
    conf, reason = scored.get(
        source.url, (0.0, "Not scored by the model; rejected by default.")
    )
    conf = max(0.0, min(1.0, conf))
    return IdentityVerdict(
        source_url=source.url,
        confidence=conf,
        decision=_decide(conf),
        reason=reason,
    )


def _parse_scores(text: str) -> dict[str, tuple[float, str]]:
    """Map source_url -> (confidence, reason). Never raises: a malformed model
    reply yields {} so every source falls through to a safe rejected default."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()

    data = _loads(cleaned)
    if data is None:
        start, end = cleaned.find("["), cleaned.rfind("]")
        if 0 <= start < end:
            data = _loads(cleaned[start : end + 1])
    if not isinstance(data, list):
        return {}

    out: dict[str, tuple[float, str]] = {}
    for item in data:
        if not isinstance(item, dict):
            continue
        url = item.get("source_url")
        if not isinstance(url, str) or not url:
            continue
        try:
            conf = float(item.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        reason = item.get("reason") if isinstance(item.get("reason"), str) else ""
        out[url] = (conf, reason)
    return out


def _loads(text: str) -> object | None:
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return None
