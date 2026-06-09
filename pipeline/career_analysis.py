"""Deterministic career-history parsing for the per-person insights layer.

The insights pass needs two structured facts the raw claims only encode as text:
- a person's FIRST employer after graduation (anchors "Still at their first
  firm" and the Origins view), and
- the START year of each role (so "first post-grad job" is well-defined).

career_history claim values are written by the collectors in a small set of
shapes (see linkedin_firecrawl / structuring):
    "Analyst at TRS (2015-2020)"
    "Partner at Acme Capital (2020-present)"
    "Managing Director at Walleye Capital (2021-2021)"
    "Analyst at Goldman"           (no dates)
    "Founder"                      (no company)
with the quote sometimes carrying "2015 - 2020 Analyst @ TRS". This module
parses both forms and never raises — an unparseable entry yields empty fields.

Pure and deterministic; unit-tested directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from enrichment_store import ClaimRow
from insights_rollup import SENIORITY_TIERS, classify_seniority_keyword

# Seniority-ladder index at/above which a title counts as "MD or above" (the
# "Director / Managing Director" rung). Mirrors the reached_md bar.
_MD_TIER_INDEX = 2

_PAREN_YEARS_RE = re.compile(r"\((\d{4})\s*[-–]\s*(\d{4}|present|current)?\)", re.IGNORECASE)
_QUOTE_YEARS_RE = re.compile(r"\b(\d{4})\s*[-–]\s*(\d{4}|present|current)\b", re.IGNORECASE)
_ANY_YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-3]\d)\b")


@dataclass(frozen=True)
class CareerEntry:
    title: str
    company: str
    start_year: int | None
    end_year: int | None  # None == ongoing/"present" or unknown


def _split_title_company(text: str) -> tuple[str, str]:
    """Split "TITLE at COMPANY" -> (title, company). No " at " -> all title."""
    # Drop a trailing "(....)" date parenthetical before splitting.
    head = re.sub(r"\s*\([^)]*\)\s*$", "", text).strip()
    # Split on the LAST " at " so "Head of Research at X at Y" keeps the firm.
    parts = re.split(r"\s+at\s+", head, flags=re.IGNORECASE)
    if len(parts) >= 2:
        return parts[0].strip(), parts[-1].strip()
    return head, ""


def _years(value: str, quote: str) -> tuple[int | None, int | None]:
    """Pull (start, end) years from the value's parenthetical, then the quote.
    end is None when the role is open-ended ("present") or absent."""
    m = _PAREN_YEARS_RE.search(value)
    if not m:
        m = _QUOTE_YEARS_RE.search(quote or "")
    if m:
        start = int(m.group(1))
        end_raw = (m.group(2) or "").lower()
        end = int(end_raw) if end_raw.isdigit() else None
        return start, end
    # No range — fall back to a single bare year if present in the value.
    single = _ANY_YEAR_RE.search(value)
    return (int(single.group(1)), None) if single else (None, None)


def parse_career_entry(value: str, quote: str = "") -> CareerEntry:
    """Parse one career_history claim into a structured entry. Never raises."""
    title, company = _split_title_company(value or "")
    start, end = _years(value or "", quote or "")
    return CareerEntry(title=title, company=company, start_year=start, end_year=end)


def career_entries(claims: list[ClaimRow]) -> list[CareerEntry]:
    """Parse all career_history claims in a person's claim set."""
    return [
        parse_career_entry(c.value, c.quote)
        for c in claims
        if c.claim_type == "career_history"
    ]


def first_post_grad_employer(
    claims: list[ClaimRow], grad_year: int | None
) -> str:
    """The company of the earliest role that began at/after graduation. Falls back
    to the earliest dated role overall, then to any role with a company. Returns
    "" when no career entry names an employer.

    Roles before grad_year are skipped (internships/student jobs) only when a
    grad_year is known AND at least one role starts at/after it — so we never
    discard a person's whole history because of an early date."""
    entries = [e for e in career_entries(claims) if e.company]
    if not entries:
        return ""

    dated = [e for e in entries if e.start_year is not None]
    if grad_year is not None and dated:
        post = [e for e in dated if e.start_year >= grad_year]
        if post:
            return min(post, key=lambda e: e.start_year).company

    if dated:
        return min(dated, key=lambda e: e.start_year).company
    return entries[0].company


def _norm_company(name: str) -> str:
    return " ".join(name.lower().replace(",", " ").split())


def num_employers(claims: list[ClaimRow]) -> int:
    """Distinct employers across the career history — a job-mobility proxy."""
    companies = {
        _norm_company(e.company) for e in career_entries(claims) if e.company
    }
    return len(companies)


def years_to_md(claims: list[ClaimRow], grad_year: int | None) -> int | None:
    """Years from graduation to the FIRST Managing-Director-or-above role (career
    velocity). None when grad year is unknown or no MD+ role has a start year.
    Clamped at 0 so a pre-graduation senior title can't go negative."""
    if grad_year is None:
        return None
    md_starts = [
        e.start_year
        for e in career_entries(claims)
        if e.start_year is not None
        and classify_seniority_keyword(e.title) in SENIORITY_TIERS
        and SENIORITY_TIERS.index(classify_seniority_keyword(e.title)) >= _MD_TIER_INDEX
    ]
    if not md_starts:
        return None
    return max(0, min(md_starts) - grad_year)


def tenure_years(start_year: int | None, ref_year: int) -> int | None:
    """Years in the current role given its start year and a reference (snapshot)
    year. None when the start year is unknown; clamped at 0."""
    if start_year is None:
        return None
    return max(0, ref_year - start_year)
