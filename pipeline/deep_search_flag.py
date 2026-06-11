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

# A résumé with fewer than this many career roles is "thin" — the case where
# the probe (2026-06-11) showed a LinkedIn read genuinely pays off (Payal 0→16,
# Bart 0→10). At/above it PDL already carries the résumé and the flaky ~189-credit
# read adds little or worse (Will: PDL 11 vs LinkedIn 4 truncated).
_MIN_CAREER_ENTRIES = 3


def should_flag_for_deep_search(b: CompletenessBreakdown) -> tuple[bool, str]:
    """Return (needs_deep, reason). Flags ONLY what a deep Firecrawl/LinkedIn read
    can actually fix: a missing current role, or a thin career history. Bio,
    press, education, and undated-role gaps are deliberately NOT flagged — a bio
    is synthesized free in the base pass, and the others don't warrant the flaky,
    expensive read. Keeping the flag tight is the whole point of the two-pass
    split: spend depth only where PDL genuinely whiffed. Rich profiles → (False, "")."""
    reasons: list[str] = []
    if not b.has_current_role:
        reasons.append("no current role")
    if b.career_entries < _MIN_CAREER_ENTRIES:
        reasons.append(f"thin career (<{_MIN_CAREER_ENTRIES} roles)")
    return (bool(reasons), "; ".join(reasons))
