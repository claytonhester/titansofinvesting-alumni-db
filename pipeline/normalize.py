"""Claim normalization and deduplication for Phase 2.

Two responsibilities:
1. smart_title() — title-case claim values so "senior investment manager"
   becomes "Senior Investment Manager" and "KKR" stays "KKR".
2. digest_claims() — deduplicate a merged claim set (e.g. Firecrawl + PDL)
   before writing to the DB, keeping the highest-confidence version of any
   duplicate and dropping exact case-insensitive matches.

Applied just before replace_claims() so the DB always holds clean data
regardless of which sources contributed.
"""
from __future__ import annotations

from enrichment_store import ClaimRow

# Words that stay lowercase unless they are the first word in the string.
_MINOR = frozenset({
    "a", "an", "the", "and", "but", "or", "for", "nor",
    "on", "at", "to", "by", "in", "of", "up", "as", "vs",
})

# Roman-numeral suffixes common in professional titles and names ("Partner III",
# "Managing Director II"). Sourced lowercase, these must read as uppercase, not
# "Iii". Single "i" is excluded — it's ambiguous and capitalizes to "I" anyway.
_ROMAN_SUFFIXES = frozenset({
    "ii", "iii", "iv", "v", "vi", "vii", "viii", "ix", "x",
})

# Claim types whose values should be title-cased.
_TITLE_CASE_TYPES = frozenset({
    "current_title",
    "current_employer",
    "career_history",
    "education",
    "location",
})


def smart_title(value: str) -> str:
    """Title-case a string with investment-domain awareness.

    Rules (in priority order):
    - All-caps tokens (KKR, TRS, LLC, LP, PE, CFO, CEO…) are preserved.
    - Mixed-case tokens already set by the source (McDonald, DeVos) are
      preserved.
    - Minor words (of, at, the, and…) are lowercased unless they are the
      first token.
    - Everything else is capitalized.

    The *quote* field is never passed here — it must stay verbatim.
    """
    if not value:
        return value
    tokens = value.strip().split()
    result: list[str] = []
    for i, tok in enumerate(tokens):
        # Strip leading/trailing punctuation to inspect the word itself.
        stripped = tok.strip(".,;:()[]\"'")
        prefix = tok[: len(tok) - len(tok.lstrip(".,;:()[]\"'"))]
        suffix = tok[len(tok.rstrip(".,;:()[]\"'")):]

        if not stripped:
            result.append(tok)
            continue

        # Preserve all-caps acronyms (>1 char, all uppercase letters/digits).
        if stripped.isupper() and len(stripped) > 1 and stripped.isalpha():
            result.append(tok)
            continue

        # Preserve already-mixed-case words (e.g. "McCallum", "DeVos").
        if any(c.isupper() for c in stripped[1:]):
            result.append(tok)
            continue

        word_lower = stripped.lower()

        # Roman-numeral suffixes read as uppercase ("iii" -> "III").
        if word_lower in _ROMAN_SUFFIXES:
            result.append(prefix + word_lower.upper() + suffix)
            continue

        # Minor words stay lowercase unless they open the string.
        if i > 0 and word_lower in _MINOR:
            result.append(prefix + word_lower + suffix)
        else:
            result.append(prefix + word_lower.capitalize() + suffix)

    return " ".join(result)


def _normalize_value(claim_type: str, value: str) -> str:
    """Apply smart_title to claim types that display as professional titles."""
    if claim_type in _TITLE_CASE_TYPES:
        return smart_title(value)
    return value


def normalize_claim(claim: ClaimRow) -> ClaimRow:
    """Return a new ClaimRow with the value title-cased where appropriate.
    The quote field is left untouched — it is a verbatim source excerpt."""
    normed = _normalize_value(claim.claim_type, claim.value)
    if normed == claim.value:
        return claim
    return ClaimRow(
        claim_type=claim.claim_type,
        value=normed,
        source_url=claim.source_url,
        quote=claim.quote,          # verbatim — never mutated
        confidence=claim.confidence,
        extraction_method=claim.extraction_method,
    )


def _confidence(claim: ClaimRow) -> float:
    """Confidence as a sortable float; a missing value floors to 0.0."""
    return claim.confidence if claim.confidence is not None else 0.0


def digest_claims(claims: list[ClaimRow]) -> list[ClaimRow]:
    """Normalize and deduplicate a merged claim set before persisting.

    Steps:
    1. Normalize case on all eligible claim values.
    2. Deduplicate: when two claims share the same (claim_type,
       case-insensitive value), keep the one with higher confidence.
       Ties go to the first seen (Firecrawl/Haiku typically comes first
       and has source-quoted evidence; PDL is secondary).

    news_mention claims are intentionally excluded from deduplication
    because multiple articles about the same person are all valid.
    """
    normalized = [normalize_claim(c) for c in claims]

    seen: dict[tuple[str, str], ClaimRow] = {}
    news: list[ClaimRow] = []

    for c in normalized:
        if c.claim_type == "news_mention":
            # Keep all news items — they are distinct articles, not duplicates.
            news.append(c)
            continue

        key = (c.claim_type, c.value.lower().strip())
        existing = seen.get(key)
        # Defensive: a malformed source could hand us a None confidence; treat it
        # as the lowest so it never wins a tie and never raises on comparison.
        if existing is None or _confidence(c) > _confidence(existing):
            seen[key] = c

    # Preserve insertion order: verified claims first, then news.
    return list(seen.values()) + news
