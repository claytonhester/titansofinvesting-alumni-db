"""Sector classification for alumni — the python mirror of web
`lib/db.ts::classifySector` / SECTOR_RULES / INDUSTRY_MAP.

Buckets both first employers (Origins) and current employers (Outcomes /
landing sectors) into one shared taxonomy, so the cohort snapshot (computed here)
and the web agree. Kept in sync with the TS tables by hand — same discipline as
normalize.py <-> normalize.ts.

Two signals, in priority order:

1. **PDL industry** (authoritative). When we know a person's LinkedIn-style
   `current_industry`, that maps far more reliably than guessing from a firm
   name — a law firm, hospital, or software shop has no finance keyword, so the
   old name-only classifier dumped them all into the catch-all. INDUSTRY_MAP is
   checked first and wins.
2. **Employer-name keywords** (fallback). For people with no industry on record
   (no PDL match), or an industry too generic to place (e.g. bare "financial
   services"), fall back to keyword-matching the firm name.

Anything still unmatched is the catch-all. The genuinely ambiguous remainder is
handed to the Haiku classifier (`insights_llm.classify_sectors`) by callers; this
module stays pure and deterministic.

First matching rule wins (rules are ordered). Pure, never raises.
"""
from __future__ import annotations

SECTOR_CATCHALL = "Other / Operating"

# Mirror of web INDUSTRY_MAP — keep identical. Each entry is (sector, substrings):
# if any substring appears in the lowercased PDL industry, that sector wins.
# Ordered: specific before generic. Bare "financial services" / "research" are
# intentionally NOT mapped (too ambiguous) — they fall through to keywords/Haiku.
INDUSTRY_MAP: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Real Estate", ("real estate", "commercial real estate", "reit")),
    ("Law / Legal", ("law practice", "legal services", "legal")),
    (
        "Technology",
        (
            "computer software", "information technology", "computer hardware",
            "internet", "semiconductor", "software", "computer networking",
            "information services", "consumer electronics",
        ),
    ),
    (
        "Healthcare & Life Sciences",
        (
            "hospital", "health care", "healthcare", "medical practice",
            "pharmaceutical", "biotechnology", "health, wellness", "mental health",
            "medical device",
        ),
    ),
    ("Insurance", ("insurance",)),
    (
        "Education & Academia",
        ("higher education", "education management", "e-learning", "edtech"),
    ),
    (
        "Government & Nonprofit",
        (
            "non-profit", "nonprofit", "government administration",
            "philanthropy", "public policy", "think tanks", "international affairs",
            "civic", "political organization",
        ),
    ),
    ("Private Equity & Credit", ("venture capital", "private equity")),
    ("Hedge Funds & Asset Mgmt", ("investment management", "asset management")),
    ("Investment Banking", ("investment banking", "banking")),
    ("Consulting", ("management consulting",)),
    ("Accounting & Audit", ("accounting",)),
    ("Energy & Real Assets", ("oil & energy", "oil", "utilities", "mining", "renewables")),
)

# Mirror of web SECTOR_RULES — keep the keyword lists identical. Matched against
# the lowercased EMPLOYER NAME when industry is absent or unmapped.
SECTOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "Investment Banking",
        (
            "goldman", "morgan stanley", "j.p. morgan", "jp morgan", "jpmorgan",
            "bank of america", "merrill", "citi", "credit suisse", "barclays",
            "ubs", "deutsche bank", "lazard", "evercore", "moelis", "jefferies",
            "houlihan", "rbc", "wells fargo", "raymond james", "piper",
            "guggenheim", "centerview",
        ),
    ),
    (
        "Consulting",
        (
            "mckinsey", "bain & company", "boston consulting", "bcg", "accenture",
            "oliver wyman", "l.e.k", "booz", "alvarez", "consulting",
        ),
    ),
    (
        "Accounting & Audit",
        ("pwc", "pricewaterhouse", "deloitte", "ernst", "kpmg", "grant thornton",
         "bdo", "ey "),
    ),
    (
        "Law / Legal",
        (
            "law firm", "law offices", "llp", "attorneys", "akin gump",
            "kirkland", "latham", "skadden", "sidley", "vinson", "baker botts",
            "jones day", "gibson dunn", "wachtell", "& feld",
        ),
    ),
    (
        "Real Estate",
        (
            "real estate", "realty", "properties", "property group", "cbre",
            "jll", "hines", "trammell crow", "american campus", "realtors",
        ),
    ),
    (
        "Private Equity & Credit",
        (
            "blackstone", "kkr", "carlyle", "apollo", "tpg", "vista", "warburg",
            "ares", "bain capital", "private equity", "capital partners",
            "holdings", "equity",
        ),
    ),
    (
        "Hedge Funds & Asset Mgmt",
        (
            "citadel", "bridgewater", "point72", "millennium", "fidelity",
            "blackrock", "vanguard", "pimco", "wellington", "capital management",
            "asset management", "investment management", "advisors",
            "capital group",
        ),
    ),
    (
        "Healthcare & Life Sciences",
        (
            "hospital", "health system", "healthcare", "health care", "clinic",
            "pharma", "biotech", "abbott", "medtronic", "pfizer", "merck",
        ),
    ),
    (
        "Technology",
        (
            "google", "microsoft", "amazon", "meta", "apple", "salesforce",
            "oracle", "nvidia", "software", "technologies", "labs", "ai",
        ),
    ),
    (
        "Insurance",
        ("insurance", "assurance", "reinsurance", "aig", "chubb", "metlife",
         "prudential", "allstate"),
    ),
    (
        "Energy & Real Assets",
        (
            "exxon", "chevron", "conocophillips", "phillips 66", "halliburton",
            "schlumberger", "encap", "quantum", "kinder morgan", "energy",
            "petroleum", "oil", "gas", "resources", "midstream",
        ),
    ),
    (
        "Education & Academia",
        ("university", "college", "school district", "academy", "institute"),
    ),
    (
        "Government & Nonprofit",
        (
            "foundation", "nonprofit", "non-profit", "department of",
            "city of", "county of", "federal", "ministry", "united nations",
        ),
    ),
)


def _match_industry(industry: str) -> str | None:
    """Return the sector for a PDL industry string, or None if unmapped."""
    s = (industry or "").lower().strip()
    if not s:
        return None
    for sector, needles in INDUSTRY_MAP:
        if any(n in s for n in needles):
            return sector
    return None


def _match_company(company: str) -> str | None:
    """Return the sector for an employer name by keyword, or None if unmatched."""
    c = (company or "").lower()
    if not c.strip():
        return None
    for sector, keywords in SECTOR_RULES:
        if any(k in c for k in keywords):
            return sector
    return None


def classify_sector(company: str, industry: str = "") -> str:
    """Bucket a person into a sector. PDL `industry` wins when it maps; otherwise
    fall back to employer-name keywords; otherwise the catch-all. Empty inputs ->
    catch-all (never raises). The ambiguous catch-all remainder is what callers
    hand to the Haiku classifier."""
    return _match_industry(industry) or _match_company(company) or SECTOR_CATCHALL


# Every sector this taxonomy can emit, in display/priority order, plus the
# catch-all last. Mirrors web SECTOR_NAMES; used to validate Haiku output.
SECTOR_NAMES: tuple[str, ...] = (
    "Investment Banking",
    "Private Equity & Credit",
    "Hedge Funds & Asset Mgmt",
    "Consulting",
    "Accounting & Audit",
    "Energy & Real Assets",
    "Real Estate",
    "Law / Legal",
    "Technology",
    "Healthcare & Life Sciences",
    "Insurance",
    "Education & Academia",
    "Government & Nonprofit",
    SECTOR_CATCHALL,
)
