"""Deterministic rule: does this profile warrant a deep (Firecrawl) re-research?

The two-pass architecture (settled 2026-06-11): a cheap base sweep enriches
everyone from PDL + a search-corrected LinkedIn URL, NO Firecrawl reads, and
marks who is still thin. A later targeted pass spends the flaky/expensive
Firecrawl reads only on the flagged subset.

This is the marker. Pure function over the completeness breakdown already
computed by compute_completeness.py — no DB, no API, no spend. The flag is
owned by compute_completeness (set AND cleared on every finalize), so a profile
that became rich in the deep pass clears itself, which is what terminates the
deep queue.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # import-only for the type hint; avoids a runtime import cycle
    from compute_completeness import CompletenessBreakdown

# Full career credit at this many dated roles; below it the résumé is thin.
_MIN_CAREER_ENTRIES = 3
# Below this overall score a profile is weak regardless of which part is missing.
_WEAK_SCORE = 60


def should_flag_for_deep_search(b: CompletenessBreakdown) -> tuple[bool, str]:
    """Return (needs_deep, reason). Flags a profile that PDL left thin enough
    that a LinkedIn read could plausibly help: missing current role, a thin or
    undated career, no bio, or a low overall completeness score. Rich profiles
    return (False, "")."""
    reasons: list[str] = []
    if not b.has_current_role:
        reasons.append("no current role")
    if b.career_entries < _MIN_CAREER_ENTRIES or b.dated_career_share < 1.0:
        reasons.append("thin/undated career")
    if not b.has_bio:
        reasons.append("no bio")
    if b.score < _WEAK_SCORE:
        reasons.append("completeness<60")
    return (bool(reasons), "; ".join(reasons))
