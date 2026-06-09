"""Deterministic identity pre-filter — record-linkage blocking before the LLM gate.

Standard entity-resolution practice (Splink, Zingg, dedupe.io) never sends every
candidate to the expensive matcher: a cheap, high-precision rule decides the
slam-dunks first, and only the ambiguous middle reaches the costly layer. Here the
costly layer is the Sonnet identity gate (`identity.resolve_identity`), which drives
most of the per-person Claude bill.

This module classifies each source against the directory anchors using ONLY
deterministic token presence — no model, no network. A source is auto-accepted here
ONLY when it carries an overwhelming multi-anchor match (exact full name AND company
AND at least one of city/school). That combination is higher-precision than an LLM
score and is exactly the signal record-linkage uses for safe auto-merge.

Crucially this is conservative on the merge side: it ONLY decides confident accepts.
It NEVER pre-rejects — anything short of a slam dunk falls through to Sonnet, so the
gate's recall and the "never auto-merge an uncertain identity" rule are preserved.
The win is twofold: fully-decided people skip the Sonnet call entirely, and partially
decided people send Sonnet a smaller prompt (fewer source snippets = fewer tokens).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from directory_hosts import is_untrusted_identity_host
from discovery import Source
from enrichment_store import DECISION_ACCEPT
from identity import AUTO_ACCEPT, IdentityVerdict, PersonAnchors

# Heuristic confidence we assign a deterministic slam-dunk. Kept at/above
# AUTO_ACCEPT so it routes as an accept, but below 1.0 to stay honest that this
# is a rule, not proof.
_PREFILTER_CONFIDENCE = max(AUTO_ACCEPT, 0.92)

# Corporate suffixes and generic tokens that carry no identifying signal — dropped
# before company/school token matching so "Acme" matches "Acme Capital LLC".
_STOPWORDS = frozenset(
    {
        "inc", "incorporated", "llc", "lp", "llp", "ltd", "limited", "co",
        "corp", "corporation", "company", "group", "holdings", "partners",
        "capital", "management", "advisors", "advisers", "associates",
        "ventures", "fund", "the", "and", "of", "university", "college",
        "school", "institute",
    }
)

# Geographic tokens that identify a place, not an institution. A school whose only
# significant token is one of these ("University of Texas" -> "texas", "Texas A&M"
# -> "texas") is named after its state/city: the token appears on any page about
# that place, so it cannot anchor a slam dunk (this is how a Boston-University /
# Morgan-Stanley namesake auto-accepted for a UT alum off a broker page echoing
# "Dallas, Texas"). Distinctive school names ("Baylor", "Rice") are unaffected.
# MAINTENANCE: this set is tuned for the Texas cohort (UT / A&M / Baylor). If the
# roster expands to other regions, add the new state/city tokens for any school
# named after its location (e.g. "california" for "University of California").
_GEO_TOKENS = frozenset(
    {
        "texas", "austin", "houston", "dallas", "antonio", "san", "new", "york",
        "california", "francisco", "los", "angeles", "chicago", "boston", "miami",
        "national", "american", "america", "global", "united", "states", "usa",
        "us", "north", "south", "east", "west", "international",
    }
)


@dataclass(frozen=True)
class PrefilterOutcome:
    """Split of a person's sources after the deterministic pass.

    `decided` are confident auto-accepts that need no Sonnet call. `ambiguous`
    are everything else, to be scored by the Sonnet gate."""

    decided: tuple[IdentityVerdict, ...]
    ambiguous: tuple[Source, ...]


def _normalize(text: str) -> str:
    """Lowercase, collapse punctuation to spaces, squeeze whitespace."""
    lowered = (text or "").lower()
    collapsed = re.sub(r"[^a-z0-9]+", " ", lowered)
    return re.sub(r"\s+", " ", collapsed).strip()


def _significant_tokens(value: str) -> list[str]:
    """Identifying tokens of an anchor — stopwords and 1-char noise removed."""
    return [
        t for t in _normalize(value).split()
        if t and t not in _STOPWORDS and len(t) > 1
    ]


def _phrase_present(phrase: str, haystack: str) -> bool:
    """Whole-phrase containment on normalized text (word-boundary safe)."""
    needle = _normalize(phrase)
    if not needle:
        return False
    return f" {needle} " in f" {haystack} "


def _all_tokens_present(value: str, haystack: str) -> bool:
    """Every significant token of `value` appears in `haystack`. Used for company
    and school, where word order and corporate suffixes vary across sources."""
    tokens = _significant_tokens(value)
    if not tokens:
        return False
    hay = f" {haystack} "
    return all(f" {t} " in hay for t in tokens)


def _source_text(source: Source) -> str:
    """Searchable normalized blob for one source: title + description + body."""
    return _normalize(" ".join((source.title, source.description, source.markdown)))


def _has_distinctive_token(value: str) -> bool:
    """True when an anchor carries at least one non-geographic identifying token —
    i.e. it names an institution, not merely a place. Used to disqualify schools
    named only after their state/city from carrying a slam dunk."""
    return any(t not in _GEO_TOKENS for t in _significant_tokens(value))


def _city_is_name_token(anchors: PersonAnchors) -> bool:
    """True when the city is just a token of the person's OWN name — e.g. a person
    named 'Austin' whose city is 'Austin'. Such a city is not an independent
    anchor: any source mentioning the name trivially 'matches' the city, so it
    must not count toward a slam dunk (this is how a namesake SEC report for a
    Utah advisor passed for a UT alum named Austin)."""
    city_tokens = _normalize(anchors.city).split()
    if not city_tokens:
        return False
    name_tokens = set(_normalize(anchors.full_name).split())
    return all(tok in name_tokens for tok in city_tokens)


def _company_is_school_placeholder(anchors: PersonAnchors) -> bool:
    """True when the roster 'company' is really the school name used as a
    placeholder (no real employer on record), e.g. company='University of Texas'
    and school='University of Texas'. Then company is NOT an independent employer
    anchor — it collapses the slam dunk to name + a single school token, which is
    far too weak to auto-accept a common name."""
    company_tokens = set(_significant_tokens(anchors.company))
    school_tokens = set(_significant_tokens(anchors.school))
    return bool(company_tokens) and company_tokens == school_tokens


def _matched_anchors(anchors: PersonAnchors, text: str) -> list[str]:
    """Which directory anchors this source's text corroborates. The city anchor is
    suppressed when it is merely a token of the person's name (see above)."""
    matched: list[str] = []
    if _phrase_present(anchors.full_name, text):
        matched.append("name")
    if _all_tokens_present(anchors.company, text):
        matched.append("company")
    if not _city_is_name_token(anchors) and _phrase_present(anchors.city, text):
        matched.append("city")
    if _has_distinctive_token(anchors.school) and _all_tokens_present(
        anchors.school, text
    ):
        matched.append("school")
    return matched


def _is_slam_dunk(matched: list[str], *, company_is_placeholder: bool) -> bool:
    """Auto-accept only on an overwhelming multi-anchor match: the exact full name
    AND a REAL company AND at least one secondary anchor (city or school). A
    company that is just the school placeholder is not a real employer anchor, so
    it cannot carry the accept — those people go to Sonnet. One or two weak
    signals are NOT enough."""
    if company_is_placeholder:
        return False
    has_secondary = "city" in matched or "school" in matched
    return "name" in matched and "company" in matched and has_secondary


def prefilter(
    anchors: PersonAnchors, sources: tuple[Source, ...]
) -> PrefilterOutcome:
    """Partition sources into deterministic auto-accepts and the ambiguous
    remainder. Never raises and never rejects — worst case every source is
    ambiguous and the behaviour is identical to having no pre-filter."""
    decided: list[IdentityVerdict] = []
    ambiguous: list[Source] = []
    company_is_placeholder = _company_is_school_placeholder(anchors)
    for source in sources:
        # Data-broker / aggregator / social hosts echo the query anchors in their
        # boilerplate, so a token match there is not evidence about the real person.
        # Never auto-accept them — route to the semantic Sonnet gate.
        if is_untrusted_identity_host(source.url):
            ambiguous.append(source)
            continue
        matched = _matched_anchors(anchors, _source_text(source))
        if _is_slam_dunk(matched, company_is_placeholder=company_is_placeholder):
            decided.append(
                IdentityVerdict(
                    source_url=source.url,
                    confidence=_PREFILTER_CONFIDENCE,
                    decision=DECISION_ACCEPT,
                    reason=(
                        "Deterministic anchor match on "
                        f"{', '.join(matched)} (pre-filter, no model)."
                    ),
                )
            )
        else:
            ambiguous.append(source)
    return PrefilterOutcome(decided=tuple(decided), ambiguous=tuple(ambiguous))
