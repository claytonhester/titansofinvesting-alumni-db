"""Haiku identity gate for PDL's career/education extras.

PDL self-disambiguates and we already accept only likelihood>=6 matches
(pdl_enrich), but a confident match can still carry a stray entry from a blended
namesake — and the user wants the deeper résumé facts PDL adds held to the same
identity bar as our public mentions (news_verify).

So this takes the PDL-derived career_history + education claims and asks Claude
Haiku, given the target's anchors (name, known employer, city, the Texas A&M
finance/investing program), which entries are CONSISTENT with being the same
person and which look like a different person was blended in. The current
title/employer/location and public links are NOT judged — those are the matched
identity itself, aligned with the query by construction.

Deliberately conservative: it drops an entry ONLY when it clearly indicates a
different person (wrong profession, wrong geography, implausible for this
profile). A plausible-but-unverifiable entry (an executive-ed degree, a board
seat) is KEPT — this is a coherence gate against blends, not ground-truth
verification we cannot do without a source page.

Batched one call per person. Never raises: any failure keeps every claim, so a
bulk loop degrades to today's "trust PDL" behavior instead of dropping real data.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from enrichment_store import ClaimRow
from structuring import HAIKU_MODEL

EXTRACTION_METHOD = "pdl+haiku-verify"

# PDL claim types whose EXTRAS we identity-gate. Current role / location / links
# are the matched identity itself and pass through untouched.
_GATED_TYPES = frozenset({"career_history", "education"})

_SYSTEM = """A data aggregator matched a person by name and returned their \
résumé. It is usually right, but a confident match can still splice in an entry \
from a DIFFERENT person who shares the name. Your job is to catch those splices.

You get the target person — an alumnus of a Texas university finance/investing \
program (Titans of Investing): their name, the employer on record, and a city — \
and a numbered list of their claimed past roles and degrees.

For each entry decide keep or drop:
- keep — it is consistent with THIS person: a finance/investing/business role, a \
degree, a board/advisory seat, or anything plausible for this professional, even \
if you cannot independently confirm it. When in doubt, KEEP.
- drop — it clearly belongs to a DIFFERENT person: a wildly unrelated profession \
(pastor, pro athlete, surgeon), a geography/era that cannot be the same person, \
or an obvious namesake splice.

Bias strongly toward keep. Only drop on a clear inconsistency, never on mere \
sparseness (a bare school name with no degree is still KEEP).

Return ONLY a JSON array, one object per entry, SAME order:
[{"index": <int>, "decision": "keep|drop", "reason": "<short>"}]"""


@dataclass(frozen=True)
class _Verdict:
    index: int
    keep: bool
    reason: str


def _build_user(name: str, employer: str, city: str, entries: list[ClaimRow]) -> str:
    lines = [
        "Target person:",
        f"  Name: {name}",
        f"  Known employer (may be outdated): {employer or '(unknown)'}",
        f"  City: {city or '(unknown)'}",
        "",
        "Entries to judge:",
    ]
    for i, c in enumerate(entries):
        lines.append(f"[{i}] {c.claim_type}: {c.value}")
    return "\n".join(lines)


def _parse(text: str, n: int) -> list[_Verdict]:
    """Parse the model's JSON array. Anything missing/malformed defaults to KEEP
    so the gate can only remove an entry on an explicit, parseable 'drop'."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    parsed: object = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("["), cleaned.rfind("]")
        if 0 <= start < end:
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                parsed = None

    by_index: dict[int, _Verdict] = {}
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            decision = str(item.get("decision", "keep")).strip().lower()
            by_index[idx] = _Verdict(idx, decision != "drop", str(item.get("reason", "")).strip())

    return [by_index.get(i, _Verdict(i, True, "no verdict — kept")) for i in range(n)]


def verify_pdl_claims(
    client: Anthropic,
    name: str,
    employer: str,
    city: str,
    claims: list[ClaimRow],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 1024,
) -> tuple[list[ClaimRow], int, int]:
    """Identity-gate the PDL career/education extras in `claims`. Returns
    (kept_claims, haiku_in, haiku_out). Non-gated claims pass through unchanged;
    gated entries judged consistent are kept. On any failure, all claims are kept
    with zero token usage."""
    gated = [c for c in claims if c.claim_type in _GATED_TYPES]
    passthrough = [c for c in claims if c.claim_type not in _GATED_TYPES]
    if not gated:
        return claims, 0, 0

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _build_user(name, employer, city, gated)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        tok_in = response.usage.input_tokens
        tok_out = response.usage.output_tokens
    except Exception:
        return claims, 0, 0

    verdicts = _parse(text, len(gated))
    kept_gated = [c for c, v in zip(gated, verdicts) if v.keep]
    # Preserve original ordering intent: passthrough (current role/links) + kept extras.
    return passthrough + kept_gated, tok_in, tok_out
