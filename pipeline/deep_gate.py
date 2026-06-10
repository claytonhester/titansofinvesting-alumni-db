"""Signal gate + Firecrawl credit ceiling for the deep-enrichment path.

Baseline discovery + article verification now run on the free Jina path, so a person
gets a full profile at zero Firecrawl credits. Firecrawl's expensive calls — the
deep career scrape and the 45–324-credit LinkedIn agent — are reserved for the
HIGH-SIGNAL few who warrant them, under a hard run-level credit ceiling.

`is_high_signal` is a cheap boolean from facts already in hand after baseline
structuring + PDL (no extra calls). `FirecrawlBudget` mirrors LinkedInBudget: a
mutable run-level counter that stops the deep block firing once the cap is reached.
"""
from __future__ import annotations

from dataclasses import dataclass

# A non-PDL person is only worth the Firecrawl deep pull when there's real corrob-
# orated web presence: at least this many identity-verified sources AND a resolved
# current employer. Tune to trade depth-coverage against credit spend.
_MIN_TRUSTED_FOR_DEEP = 2


def is_high_signal(
    pdl_matched: bool, trusted_count: int, has_current_employer: bool
) -> bool:
    """True when a person warrants the billed Firecrawl deep path. A confident PDL
    match is sufficient on its own (strong identity + firmographics); otherwise we
    require real verified web presence (>= _MIN_TRUSTED_FOR_DEEP sources) AND a
    resolved current employer. A thin, no-match person stops at the free baseline —
    Firecrawl won't find what isn't there."""
    if pdl_matched:
        return True
    return trusted_count >= _MIN_TRUSTED_FOR_DEEP and has_current_employer


@dataclass(frozen=True)
class FirecrawlDecision:
    fire: bool
    reason: str


class FirecrawlBudget:
    """Run-level hard ceiling on Firecrawl deep-path credits. Mutable on purpose —
    it threads through the per-person loop accumulating spend. Like LinkedInBudget,
    the only dependable control is pre-flight: stop firing once the budget is spent
    (one in-flight call can still overshoot by its own cost)."""

    def __init__(self, total_credits: int) -> None:
        self.remaining = max(0, total_credits)

    def decide(self) -> FirecrawlDecision:
        if self.remaining <= 0:
            return FirecrawlDecision(False, "Firecrawl deep-path budget spent")
        return FirecrawlDecision(True, "budget available")

    def charge(self, credits: int) -> None:
        self.remaining = max(0, self.remaining - (credits or 0))
