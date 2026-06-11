"""Single source of truth for non-trustworthy host classification.

Data-broker / people-directory aggregators and social-noise networks parrot the
search query (a person's name, employer, city, school) in their page title and
boilerplate for SEO. Any token-presence check then "corroborates" anchors that the
real profile body may contradict — so these hosts must never carry a deterministic
auto-accept; they are routed to the semantic (Sonnet) gate instead.

Historically each module kept its own copy of this set (`company_enrich._NON_FIRM_HOSTS`,
`news_curate._SOCIAL_HOSTS`/`_DIRECTORY_HOSTS`, web `link-quality.DROP_HOSTS`). Those
drifted. New code should import from here; the legacy copies are being migrated.
"""
from __future__ import annotations

from urllib.parse import urlparse

# People-directory / data-broker aggregators: a "basic overview profile", never a
# first-party or editorial source.
DIRECTORY_HOSTS: frozenset[str] = frozenset(
    {
        "theorg.com", "advisorcheck.com", "indyfin.com", "getwarmer.com",
        "crunchbase.com", "zoominfo.com", "rocketreach.co", "signalhire.com",
        "pitchbook.com", "spokeo.com", "comparably.com", "clay.earth",
        "wwana.com", "thealumniassociation.com", "wiza.co", "signal.nfx.com",
        "me.sh", "evalyze.ai", "lusha.com", "apollo.io", "contactout.com",
        "leadiq.com", "seamless.ai", "usebadges.com", "advisor.investedbetter.com",
        "zoomgov.com",
    }
)

# Public-records / government-salary databases. A name here is a transparency
# disclosure, not editorial news — and surfacing a person's specific salary is a
# privacy/tone problem. Dropped from the news feed and the web link/source panels.
PUBLIC_RECORDS_HOSTS: frozenset[str] = frozenset(
    {
        "texastaxpayers.com", "governmentsalaries.com", "govsalaries.com",
        "openpayrolls.com", "transparentcalifornia.com", "openthebooks.com",
    }
)

# Social-noise networks: a presence here is not a professional identity source.
SOCIAL_HOSTS: frozenset[str] = frozenset(
    {
        "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
        "youtube.com", "youtu.be", "klout.com", "foursquare.com", "pinterest.com",
        "tiktok.com", "threads.net", "reddit.com", "medium.com",
    }
)

# Hosts that must not anchor a deterministic identity auto-accept. Brokers (echo
# the query) and social networks (a namesake's handle matches just as well).
UNTRUSTED_IDENTITY_HOSTS: frozenset[str] = DIRECTORY_HOSTS | SOCIAL_HOSTS

# Hosts that can NEVER be editorial news, whatever a discovery source vouches.
# Broker/SEO-echo directories (wwana.com et al.) parrot the queried name, so a
# "press hit" there is a scraped listing about nobody in particular; public-records
# salary lookups are disclosures, not stories. Enforced in the Sonar news path
# (news_score.is_aggregator_domain) and the curator (news_curate.news_items) so
# these domains can't become news_mention claims or news_curated rows — the
# wwana.com / Ricardo Lopez echo got through when only the identity gate knew them.
NON_NEWS_HOSTS: frozenset[str] = DIRECTORY_HOSTS | PUBLIC_RECORDS_HOSTS


def full_host(url_or_host: str) -> str:
    """Full host of a URL: scheme/path/`www.` stripped, lowercased, but ALL
    sub-domains kept. 'app.apollo.io' -> 'app.apollo.io'. '' when unparseable."""
    raw = (url_or_host or "").strip().lower()
    if not raw:
        return ""
    if "//" not in raw:
        raw = "//" + raw  # let urlparse treat a bare host as a netloc
    try:
        host = urlparse(raw).hostname or ""
    except ValueError:
        return ""
    return host.removeprefix("www.")


def registrable_host(url_or_host: str) -> str:
    """Registrable domain of a URL: as `full_host` but with sub-domains stripped,
    so 'app.apollo.io' -> 'apollo.io'. Collapsing sub-domains is what catches
    `app.`/`profiles.`/`api.` broker sub-domains of a bare-domain set entry.

    Keeps the last two labels — correct for the .com/.org/.co/.net/.earth/.ai/.sh/
    .xyz hosts in this corpus; multi-label public suffixes (.co.uk) are not present.
    """
    host = full_host(url_or_host)
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def is_untrusted_identity_host(url: str) -> bool:
    """True when a source URL is a broker/aggregator/social host that must not
    deterministically auto-accept an identity. Tests BOTH the full host and the
    registrable domain, because the set holds bare domains ('apollo.io') AND broker
    sub-domains ('signal.nfx.com', 'advisor.investedbetter.com') whose registrable
    form is not itself a broker — so checking only one form would miss half of them.
    """
    host = full_host(url)
    if not host:
        return False
    return host in UNTRUSTED_IDENTITY_HOSTS or registrable_host(host) in UNTRUSTED_IDENTITY_HOSTS


def is_non_news_host(url: str) -> bool:
    """True when a URL's host is a broker/SEO-echo directory or public-records
    site that must never surface as news (claim or curated row). Same dual
    full-host + registrable-domain test as the identity check, for the same
    reason: the set mixes bare domains and broker sub-domains."""
    host = full_host(url)
    if not host:
        return False
    return host in NON_NEWS_HOSTS or registrable_host(host) in NON_NEWS_HOSTS
