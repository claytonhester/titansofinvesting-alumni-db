"""Unified per-run research policy: which gate CRITERIA apply to a person.

Before this module, four independent gatekeepers each decided "is this person
worth the spend?" with their own criteria (deep_gate.is_high_signal, the
LinkedIn gap-gate profile_needs_linkedin, plus the two budgets), and operator
overrides like --force-deep opened one gate while silently leaving the next
shut — the Bart Howe case: a "complete-looking" profile skipped the LinkedIn
agent, so its stale titles and missing dates were never refreshed.

Policy governs gate CRITERIA only. The spend ceilings (LinkedInBudget,
FirecrawlBudget, --max-credits, --max-usd) stay active under every policy —
an open gate never means unbounded spend.

    BULK     today's pipeline exactly: every gate's criteria enforce.
             For first-pass cohort runs where credit efficiency matters.
    DEEP     the deep Firecrawl path fires for every target (is_high_signal
             overridden) but the LinkedIn gap-gate still applies.
             Replaces the --force-deep bool.
    REFRESH  DEEP + the LinkedIn "profile already complete" skip is bypassed:
             the agent fires to verify/upgrade even rich profiles.
             For re-research passes over already-enriched people.
"""
from __future__ import annotations

from enum import Enum


class ResearchPolicy(str, Enum):
    BULK = "bulk"
    DEEP = "deep"
    REFRESH = "refresh"

    @classmethod
    def parse(cls, raw: str) -> "ResearchPolicy":
        """Parse a CLI string; raises ValueError naming the valid choices."""
        try:
            return cls((raw or "").strip().lower())
        except ValueError:
            valid = ", ".join(p.value for p in cls)
            raise ValueError(f"unknown research policy {raw!r} (valid: {valid})")


def force_deep_path(policy: ResearchPolicy) -> bool:
    """True when the deep Firecrawl path (career scrape / LinkedIn / news)
    should fire regardless of is_high_signal."""
    return policy in (ResearchPolicy.DEEP, ResearchPolicy.REFRESH)


def bypass_linkedin_gap_gate(policy: ResearchPolicy) -> bool:
    """True when the LinkedIn agent should fire even for a profile that LOOKS
    complete (profile_needs_linkedin bypassed). Only REFRESH: the whole point
    of a refresh pass is upgrading titles/dates on complete-looking profiles."""
    return policy is ResearchPolicy.REFRESH
