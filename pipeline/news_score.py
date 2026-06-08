"""Precision scoring for unverified news hits.

A name search (GNews or GDELT) returns articles that *might* be about our person
or might be a namesake. With only a name + an (often old) employer to go on, we
can't be certain — but we can score how plausible a hit is from cheap signals:

  - the employer is co-mentioned in the title/snippet (strongest tie),
  - the article appears in a finance/press outlet,
  - a finance/role keyword appears,
  - the person's full name appears in the headline itself.

This stays a heuristic — it raises precision, it does not prove identity — so a
scored hit is still surfaced as "unverified" on the web side.
"""
from __future__ import annotations

from dataclasses import dataclass

# Reputable finance / business press + the wires that carry fund and exec news.
_FINANCE_DOMAINS = frozenset({
    "bloomberg.com", "reuters.com", "wsj.com", "ft.com", "cnbc.com",
    "barrons.com", "marketwatch.com", "forbes.com", "fortune.com",
    "businesswire.com", "prnewswire.com", "globenewswire.com",
    "institutionalinvestor.com", "hedgeweek.com", "pehub.com", "fnlondon.com",
    "pionline.com", "citywire.com", "bizjournals.com", "axios.com",
    "businessinsider.com", "investing.com", "thedeal.com", "privateequityinternational.com",
})

# People-search / data-broker / directory sites: high namesake noise, low
# editorial value. A name lands on these by SEO, not because the article is
# about our person — so a hit here is demoted, never "confident".
_AGGREGATOR_DOMAINS = frozenset({
    "idcrawl.com", "officialusa.com", "spokeo.com", "rocketreach.co",
    "zoominfo.com", "datanyze.com", "contactout.com", "signalhire.com",
    "beenverified.com", "thatsthem.com", "peoplefinders.com", "whitepages.com",
    "fastpeoplesearch.com", "mylife.com", "radaris.com", "nuwber.com",
    "clustrmaps.com", "leadiq.com", "apollo.io", "lusha.com",
    "marketscreener.com", "bbb.org",
})

# Employer tokens too generic to prove a co-mention on their own.
_GENERIC_TOKENS = frozenset({
    "capital", "group", "partners", "management", "advisors", "advisers",
    "fund", "funds", "asset", "holdings", "company", "co", "llc", "lp",
    "inc", "the", "associates", "ventures", "and", "of",
})


def normalize_domain(url_or_domain: str) -> str:
    """Reduce a URL or domain to its bare host: drop scheme, path, and 'www.'."""
    value = (url_or_domain or "").strip().lower()
    if "//" in value:
        value = value.split("//", 1)[1]
    value = value.split("/", 1)[0]
    if value.startswith("www."):
        value = value[4:]
    return value


def _domain_in_set(url_or_domain: str, domains: frozenset[str]) -> bool:
    """Match a host against a set, allowing subdomains (es.marketscreener.com
    matches marketscreener.com)."""
    host = normalize_domain(url_or_domain)
    return any(host == d or host.endswith("." + d) for d in domains)


def is_finance_domain(url_or_domain: str) -> bool:
    return _domain_in_set(url_or_domain, _FINANCE_DOMAINS)


def is_aggregator_domain(url_or_domain: str) -> bool:
    return _domain_in_set(url_or_domain, _AGGREGATOR_DOMAINS)


def _significant_employer_tokens(employer: str | None) -> list[str]:
    if not employer:
        return []
    cleaned = employer.strip()
    if not cleaned or cleaned == "(unknown)":
        return []
    return [
        tok.lower()
        for tok in cleaned.replace(",", " ").split()
        if len(tok) >= 4 and tok.lower() not in _GENERIC_TOKENS
    ]


def has_meaningful_employer(employer: str | None) -> bool:
    """True when the employer has at least one non-generic, disambiguating word —
    i.e. it is worth AND-ing into a query. ("Capital Group" is not; "Kaspar" is.)"""
    return len(_significant_employer_tokens(employer)) > 0


def employer_comention(text: str, employer: str | None) -> bool:
    """True when a non-generic word from the employer appears in the text."""
    tokens = _significant_employer_tokens(employer)
    if not tokens:
        return False
    haystack = (text or "").lower()
    return any(tok in haystack for tok in tokens)


@dataclass(frozen=True)
class MentionScore:
    """Transparent breakdown of why a hit is (or isn't) plausibly our person.

    Tiers, by how much identity certainty the signals give us:
    - ``confident``: the person's name AND their employer co-occur in the same
      result, on a non-aggregator page. With only name + one employer to anchor
      on, this co-occurrence is the strongest cheap proof it's the right person.
    - ``plausible``: the person is named on a finance/press page, but the known
      employer isn't there to confirm it — could be them or a namesake. These
      are the candidates worth an LLM verification pass, not auto-trust.
    """

    name_present: bool        # full name appears in title or snippet
    employer_comention: bool  # a distinctive employer word appears in title/snippet
    finance_domain: bool
    aggregator_domain: bool

    @property
    def confident(self) -> bool:
        return self.name_present and self.employer_comention and not self.aggregator_domain

    @property
    def plausible(self) -> bool:
        return (
            not self.confident
            and self.name_present
            and self.finance_domain
            and not self.aggregator_domain
        )

    @property
    def score(self) -> int:
        """Ranking total (not a probability). Aggregator pages are pushed down."""
        total = (
            (1 if self.name_present else 0)
            + (2 if self.employer_comention else 0)
            + (1 if self.finance_domain else 0)
        )
        return max(0, total - (2 if self.aggregator_domain else 0))


def score_mention(
    *,
    name: str,
    employer: str | None,
    title: str,
    snippet: str = "",
    domain: str = "",
) -> MentionScore:
    """Score one hit against a person. ``snippet`` may be empty (e.g. GDELT's
    title-only artlist), which weakens name/employer co-occurrence — that is why
    snippet-bearing sources (Perplexity, GNews) disambiguate far better."""
    body = f"{title} {snippet}".strip().lower()
    name_lc = (name or "").strip().lower()
    return MentionScore(
        name_present=bool(name_lc) and name_lc in body,
        employer_comention=employer_comention(body, employer),
        finance_domain=is_finance_domain(domain),
        aggregator_domain=is_aggregator_domain(domain),
    )
