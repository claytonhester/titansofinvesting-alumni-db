"""LLM reconciliation pass: merge a person's multi-source résumé claims into one
clean, conflict-resolved set.

`digest_claims` (normalize.py) only collapses *exact* case-insensitive
duplicates. It cannot tell that "Analyst, TRS" and "Investment Analyst at Teacher
Retirement System of Texas (2015–2018)" are the same job, and it cannot pick a
winner when PDL says one current employer and a live bio page says another.

This pass does. It hands Claude Haiku the numbered list of a person's *résumé*
claims (career, education, current role, location, bio — NOT public mentions) and
asks it to group same-real-world-fact claims and emit one canonical, best-attested
version of each. Provenance is preserved: each reconciled claim keeps the
source_url / quote / confidence of its most authoritative member, so every fact
stays human-verifiable. The model may *merge* details that appear across a group's
members but may never invent a title, employer, date, school, or location.

Design guarantees:
- **Never invents** — canonical values are constrained to information present in
  the grouped claims (enforced by prompt + we only re-attach existing provenance).
- **Never drops** — any claim the model fails to mention is kept verbatim.
- **Never raises** — on any error (network, bad JSON, model drift) it returns the
  input claims unchanged, so the caller's existing digest_claims still runs.
- **No-op on thin data** — fewer than two résumé claims → no API call.

Cost: one Haiku call per person (~$0.004). Mentions/links pass straight through.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from enrichment_store import ClaimRow
from normalize import smart_title
from structuring import HAIKU_MODEL

RECONCILE_METHOD_SUFFIX = "+reconciled"

# Claim types that describe résumé facts and benefit from semantic reconciliation.
# Everything else (public_links, news_mention, ...) passes through untouched —
# multiple distinct mentions are all valid and must not be merged.
_RECONCILE_TYPES = frozenset({
    "career_history",
    "education",
    "current_title",
    "current_employer",
    "location",
    "short_bio",
})

# Types where exactly one value can be true at a time; the model must pick one.
_SINGLE_VALUE_TYPES = frozenset({
    "current_title",
    "current_employer",
    "location",
    "short_bio",
})

_SYSTEM = """You merge a person's résumé claims, gathered from multiple sources, \
into one clean, de-duplicated set. Sources disagree on phrasing, completeness, and \
freshness — your job is to RECONCILE, never to invent.

You receive a numbered list of claims, one per line: [index] type | value | source.

Group the claims that describe the SAME real-world fact (the same job, the same \
degree, the same current employer). For each group, output one reconciled fact:
- "claim_type": the fact's type. If members mix types, use the most specific.
- "value": the single best phrasing. You MAY combine details that appear across \
the group's members (e.g. take the title from one member and the dates from \
another). You MUST NOT add any title, employer, date, school, or location that \
does not already appear in the listed members. Prefer the most complete, \
professionally-cased version.
- "members": the list of indices in this group.
- "primary": the ONE member index whose source is most authoritative — an \
official company/regulatory page over an aggregator, a complete entry over a \
sparse one.

Rules:
- current_employer / current_title: the person has ONE primary current role. \
Pick the freshest, best-attested value for it and fold in only claims that are \
that SAME role phrased differently or with stale data. If a claim names a \
genuinely DIFFERENT organization or role (a board seat, advisory role, side \
venture), do NOT absorb it — emit it as its own career_history fact so it is \
PRESERVED. A board seat is not the primary job; it is history, not lost.
- location / short_bio: output exactly one fact, choosing the best value.
- List types (career_history, education): output one fact per DISTINCT real-world \
entry. Two phrasings of one job = one fact. Two different jobs = two facts.
- Every input index must appear in exactly one group. NEVER drop a claim; if a \
claim does not belong with any other, emit it as its own single-member fact.
- If unsure whether two claims are the same fact, keep them separate.

Return ONLY a JSON object, no prose:
{"facts": [{"claim_type": "...", "value": "...", "members": [int, ...], "primary": int}]}"""


# Generic role/title/corp words carry no identity — two different companies can
# share "Chief Financial Officer". Stripped before the overlap guard so a merge is
# only allowed when members share a DISTINCTIVE token (a company/school name).
_GENERIC_RECONCILE_TOKENS = frozenset({
    "chief", "financial", "officer", "executive", "managing", "director",
    "president", "vice", "senior", "junior", "analyst", "associate", "partner",
    "investment", "banking", "board", "member", "committee", "manager",
    "portfolio", "head", "founder", "owner", "principal", "advisor", "adviser",
    "consultant", "intern", "internship", "secondee", "professor", "adjunct",
    "group", "capital", "management", "partners", "company", "holdings", "fund",
    "funds", "asset", "ventures", "the", "and", "of", "at", "for", "from",
    "bachelor", "master", "degree", "science", "arts", "business",
    "administration", "finance", "university", "college", "school",
})


def _significant_tokens(value: str) -> set[str]:
    """Distinctive (non-generic, length≥4) tokens of a claim value — the part that
    actually names an organization or institution."""
    out: set[str] = set()
    for raw in (value or "").lower().replace(",", " ").replace("/", " ").split():
        tok = raw.strip(".()[]'\"-:;|&")
        if len(tok) >= 4 and tok.isalpha() and tok not in _GENERIC_RECONCILE_TOKENS:
            out.add(tok)
    return out


@dataclass(frozen=True)
class _Decision:
    claim_type: str
    value: str
    members: tuple[int, ...]
    primary: int


def _partition(claims: list[ClaimRow]) -> tuple[list[ClaimRow], list[ClaimRow]]:
    """Split into (résumé claims to reconcile, passthrough claims to leave alone)."""
    resume = [c for c in claims if c.claim_type in _RECONCILE_TYPES]
    passthrough = [c for c in claims if c.claim_type not in _RECONCILE_TYPES]
    return resume, passthrough


def _build_user(resume: list[ClaimRow]) -> str:
    lines = ["Claims to reconcile:"]
    for i, c in enumerate(resume):
        src = _short_source(c)
        lines.append(f"[{i}] {c.claim_type} | {c.value} | {src}")
    return "\n".join(lines)


def _short_source(c: ClaimRow) -> str:
    """A compact provenance hint for the model: the source host or the method."""
    url = (c.source_url or "").strip()
    if url:
        host = url.split("//", 1)[-1].split("/", 1)[0]
        return host[4:] if host.startswith("www.") else host
    return c.extraction_method or "(no source)"


def _parse_decisions(text: str, n: int) -> list[_Decision]:
    """Parse the model's JSON into validated decisions. Indices out of range are
    dropped; a primary not among members falls back to the first member. Returns
    [] if the payload is unusable so the caller keeps the original claims."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    obj: object = None
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                obj = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                return []
    if not isinstance(obj, dict):
        return []

    decisions: list[_Decision] = []
    for fact in obj.get("facts") or []:
        if not isinstance(fact, dict):
            continue
        members = tuple(
            i for i in (fact.get("members") or []) if isinstance(i, int) and 0 <= i < n
        )
        if not members:
            continue
        value = str(fact.get("value") or "").strip()
        if not value:
            continue
        claim_type = str(fact.get("claim_type") or "").strip()
        primary = fact.get("primary")
        if not isinstance(primary, int) or primary not in members:
            primary = members[0]
        decisions.append(_Decision(claim_type, value, members, primary))
    return decisions


def _apply(resume: list[ClaimRow], decisions: list[_Decision]) -> list[ClaimRow]:
    """Build reconciled ClaimRows from decisions, preserving each group's primary
    provenance and re-casing the canonical value. Any résumé claim not covered by
    a decision is kept verbatim so nothing is ever lost."""
    rows: list[ClaimRow] = []
    covered: set[int] = set()
    for d in decisions:
        canon_tokens = _significant_tokens(d.value)
        # Overlap guard: a member may only be ABSORBED into this group's canonical
        # value if it shares a distinctive token with it (or carries none of its
        # own — e.g. a bare title we can't disambiguate). Members that name a
        # different organization are split back out so a wrong LLM merge can never
        # erase a real company/school. Single-member groups are never split.
        if len(d.members) > 1 and canon_tokens:
            absorbed = [
                m for m in d.members
                if not _significant_tokens(resume[m].value)
                or _significant_tokens(resume[m].value) & canon_tokens
            ]
            split_out = [m for m in d.members if m not in absorbed]
        else:
            absorbed, split_out = list(d.members), []

        primary = resume[d.primary] if d.primary in absorbed else resume[absorbed[0]]
        claim_type = d.claim_type if d.claim_type in _RECONCILE_TYPES else primary.claim_type
        merged = len(absorbed) > 1
        method = (
            primary.extraction_method + RECONCILE_METHOD_SUFFIX
            if merged and not primary.extraction_method.endswith(RECONCILE_METHOD_SUFFIX)
            else primary.extraction_method
        )
        rows.append(
            ClaimRow(
                claim_type=claim_type,
                value=smart_title(d.value) if claim_type != "short_bio" else d.value,
                source_url=primary.source_url,
                quote=primary.quote,
                confidence=primary.confidence,
                extraction_method=method,
            )
        )
        covered.update(absorbed)
        # Re-emit any split-out member verbatim — its own value, type, provenance.
        for m in split_out:
            rows.append(resume[m])
            covered.add(m)

    # Safety net: never silently drop a claim the model forgot to group.
    for i, c in enumerate(resume):
        if i not in covered:
            rows.append(c)
    return rows


def reconcile_claims(
    client: Anthropic,
    full_name: str,
    claims: list[ClaimRow],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 2048,
) -> tuple[list[ClaimRow], int, int]:
    """Reconcile one person's claims. Returns (claims, haiku_in, haiku_out).

    Mentions/links pass through untouched; résumé facts are grouped and
    canonicalized by one Haiku call. On any failure or thin data, returns the
    input unchanged with zero token usage — the caller should still run
    digest_claims afterward for final casing + exact-dedupe."""
    resume, passthrough = _partition(claims)
    if len(resume) < 2:
        return claims, 0, 0

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _build_user(resume)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        tok_in = response.usage.input_tokens
        tok_out = response.usage.output_tokens
    except Exception:
        return claims, 0, 0

    decisions = _parse_decisions(text, len(resume))
    if not decisions:
        return claims, tok_in, tok_out

    reconciled = _apply(resume, decisions)
    return reconciled + passthrough, tok_in, tok_out
