"""Small per-person derived signals that don't belong with career parsing:
advanced-degree detection and Texas geography. Pure and deterministic.

- has_advanced_degree: did they earn a graduate degree (MBA/JD/MS/MA/PhD/MD/LLM)
  beyond the undergraduate Titans degree? Read off the education claim text.
- left_texas: is their CURRENT location outside Texas? The program's schools are
  all Texas, so "left Texas" is a meaningful mobility signal. Unknown current
  location → None (we don't guess).
"""
from __future__ import annotations

import re

# Graduate-degree markers. Word-boundaried so "BA" inside "MBA" or "Baylor"
# doesn't trip the undergrad exclusions, and "ba"/"bba"/"bs" undergrad degrees
# never count as advanced.
_ADVANCED_DEGREE_RE = re.compile(
    r"\b(mba|j\.?d\.?|ll\.?m|ph\.?d|m\.?d\b|m\.?s\b|m\.?a\b|masters?|master\s+of|doctor(ate)?)\b",
    re.IGNORECASE,
)

# Texas markers: the state, its abbreviation, and the program's home cities.
_TEXAS_TOKENS = (
    "texas", " tx", ", tx", "tx ",
    "austin", "houston", "dallas", "san antonio", "fort worth", "college station",
    "waco", "el paso", "plano", "irving", "frisco", "the woodlands", "sugar land",
    "lubbock", "round rock", "midland", "galveston",
)


def has_advanced_degree(education_texts: list[str]) -> bool:
    """True when any education line names a graduate degree."""
    return any(_ADVANCED_DEGREE_RE.search(t or "") for t in education_texts)


def is_texas(location: str) -> bool:
    """True when a location string looks like it's in Texas."""
    loc = f" {(location or '').lower()} "
    return any(tok in loc for tok in _TEXAS_TOKENS)


def left_texas(current_location: str) -> bool | None:
    """True when the current location is known AND outside Texas; False when known
    and in Texas; None when the current location is unknown (we don't assume)."""
    if not current_location or not current_location.strip():
        return None
    return not is_texas(current_location)
