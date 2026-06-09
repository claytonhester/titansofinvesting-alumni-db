"""Sector classification for employer names — the python mirror of web
`lib/db.ts::classifySector` / SECTOR_RULES.

Used to bucket both first employers (Origins) and current employers (Outcomes /
landing sectors) into the same six finance-relevant sectors, so the cohort
snapshot (computed here, in python) and the web agree on the buckets. Kept in
sync with the TS table by hand — same discipline as normalize.py ↔ normalize.ts.

First matching sector wins (rules are ordered); anything unmatched is the
catch-all. Pure and deterministic.
"""
from __future__ import annotations

SECTOR_CATCHALL = "Other / Operating"

# Mirror of web SECTOR_RULES — keep the keyword lists identical.
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
            "mckinsey", "bain", "boston consulting", "bcg", "accenture",
            "oliver wyman", "l.e.k", "booz", "alvarez", "ats", "consulting",
        ),
    ),
    (
        "Accounting & Audit",
        ("pwc", "pricewaterhouse", "deloitte", "ernst", "kpmg", "grant thornton",
         "bdo", "ey"),
    ),
    (
        "Private Equity & Credit",
        (
            "blackstone", "kkr", "carlyle", "apollo", "tpg", "vista", "warburg",
            "ares", "bain capital", "private equity", "capital partners",
            "partners", "holdings", "equity",
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
        "Energy & Real Assets",
        (
            "exxon", "chevron", "conocophillips", "phillips 66", "halliburton",
            "schlumberger", "encap", "quantum", "kinder morgan", "energy",
            "petroleum", "oil", "gas", "resources", "midstream",
        ),
    ),
)


def classify_sector(company: str) -> str:
    """Bucket an employer name into one of the six sectors, or the catch-all.
    Empty/blank input → catch-all (never raises)."""
    c = (company or "").lower()
    if not c.strip():
        return SECTOR_CATCHALL
    for sector, keywords in SECTOR_RULES:
        if any(k in c for k in keywords):
            return sector
    return SECTOR_CATCHALL
