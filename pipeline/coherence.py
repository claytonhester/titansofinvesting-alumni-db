"""Deterministic internal-consistency checks for one person's claim set.

The scorecard's Coherence category. Reference-free — judges whether the assembled
facts hang together AS ONE PERSON, no LLM, no answer key. Each rule is a pure
function returning (ok, detail); coherence_report aggregates them into a 0-100
score plus the list of failures and a hard-fail (P0) flag for impossible data
(future dates) that should cap the batch grade regardless of everything else.

Reuses career_analysis for all date/company parsing so the rules see exactly what
the rest of the pipeline sees (no second, drifting parser).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from career_analysis import _CORP_SUFFIXES, _norm_company, career_entries
from enrichment_store import ClaimRow

_TRAILING_PAREN = re.compile(r"\s*\([^)]*\)\s*$")

# Trade-name words that drop off between a firm's legal name and its display
# name ('Sage Advisory Services' on SEC vs 'Sage Advisory' on LinkedIn). Used in
# COMPARISONS only — never affects the legal-suffix list the career parser uses.
_TRADE_SUFFIXES = frozenset({"services"})


def _company_key(name: str) -> str:
    """Normalized company name for coherence comparisons only (stored values are
    untouched): drop a trailing parenthetical acronym ('American Campus
    Communities (ACC)'), a trailing corporate suffix ('Lenox Park Solutions,
    Inc.'), and a trailing trade-name word ('Sage Advisory Services') so the
    same employer compares equal across sources."""
    norm = _norm_company(_TRAILING_PAREN.sub("", name))
    parts = norm.split()
    while parts and parts[-1].rstrip(".") in (_CORP_SUFFIXES | _TRADE_SUFFIXES):
        parts.pop()
    return " ".join(parts)


@dataclass(frozen=True)
class CoherenceReport:
    score: int                       # 0-100 for this person
    failures: tuple[tuple[str, str], ...]  # (rule_name, detail)
    p0: bool                         # impossible data (future date) — hard gate


def _current_values(claims: list[ClaimRow], claim_type: str) -> list[str]:
    return [c.value for c in claims if c.claim_type == claim_type]


def exactly_one_current_role(claims: list[ClaimRow]) -> tuple[bool, str]:
    """At most one current_employer and one current_title. clean_profile should
    guarantee this; a violation means a dedupe miss or a namesake splice."""
    emp = _current_values(claims, "current_employer")
    ttl = _current_values(claims, "current_title")
    if len(emp) > 1:
        return False, f"{len(emp)} current employers: {', '.join(emp[:3])}"
    if len(ttl) > 1:
        return False, f"{len(ttl)} current titles: {', '.join(ttl[:3])}"
    return True, ""


def current_employer_in_history(claims: list[ClaimRow]) -> tuple[bool, str]:
    """The current employer should appear as the most-recent career entry. Only
    checked when both a current employer and a dated history exist; an open-ended
    ('present') entry or the latest end-year wins as 'most recent'."""
    emp = _current_values(claims, "current_employer")
    entries = [e for e in career_entries(claims) if e.company]
    if not emp or not entries:
        return True, ""
    cur = _company_key(emp[0])
    if not cur:
        return True, ""
    # Senior people hold CONCURRENT open-ended roles (a day job + a board seat +
    # an adjunct professorship). The current employer is coherent as long as it is
    # one of those active roles — not necessarily the latest-started one.
    open_ended = [e for e in entries if e.start_year is not None and e.end_year is None]
    if open_ended:
        if any(_company_key(e.company) == cur for e in open_ended):
            return True, ""
        names = ", ".join(sorted({e.company for e in open_ended if e.company}))
        return False, f"current '{emp[0]}' not among active roles ({names[:60]})"
    # No open-ended roles at all: fall back to the latest dated role.
    dated = [e for e in entries if e.end_year is not None or e.start_year is not None]
    if not dated:
        return True, ""
    recent = max(dated, key=lambda e: e.end_year or e.start_year or 0)
    if _company_key(recent.company) and _company_key(recent.company) != cur:
        return False, f"current '{emp[0]}' != latest history '{recent.company}'"
    return True, ""


def no_zero_duration_dupes(claims: list[ClaimRow]) -> tuple[bool, str]:
    """A single-year role (2018-2018) duplicating a longer/open-ended role at the
    same employer is a scrape artifact, not a second job."""
    entries = career_entries(claims)
    zero = [e for e in entries if e.start_year is not None and e.start_year == e.end_year]
    for z in zero:
        zc = _company_key(z.company)
        for e in entries:
            if e is z or _company_key(e.company) != zc:
                continue
            longer = (e.end_year is None) or (
                e.end_year is not None and e.start_year is not None
                and e.end_year > e.start_year
            )
            if longer:
                return False, f"zero-duration '{z.company} ({z.start_year}-{z.end_year})' dups a longer role"
    return True, ""


def no_future_dates(claims: list[ClaimRow], now_year: int) -> tuple[bool, str]:
    """No start/end year after the current year — impossible data (P0)."""
    for e in career_entries(claims):
        for y in (e.start_year, e.end_year):
            if y is not None and y > now_year:
                return False, f"future date {y} (now {now_year})"
    return True, ""


def has_dated_career(claims: list[ClaimRow]) -> tuple[bool, str]:
    """At least one career entry carries a year — an all-undated history is the
    Bart-Howe failure (titles with no dates)."""
    if not any(c.claim_type == "career_history" for c in claims):
        return True, ""  # no career to date; coverage's problem, not coherence's
    dated = any(
        e.start_year is not None or e.end_year is not None
        for e in career_entries(claims)
    )
    return (dated, "" if dated else "career history present but fully undated")


def coherence_report(
    claims: list[ClaimRow], grad_year: int | None, now_year: int
) -> CoherenceReport:
    """Run every rule. Score = 100 minus an even share per failed rule; a
    future-date failure also sets p0 so the scorecard can cap the batch grade.

    grad_year is accepted for API stability and future grad-anchored rules; it is
    intentionally unused right now — on this dataset grad_year is the Titans-class
    year (often a graduate program), so a career predating it is normal, not a
    coherence failure. See the dropped grad_before_career rule."""
    _ = grad_year
    checks = [
        ("one_current_role", exactly_one_current_role(claims)),
        ("employer_in_history", current_employer_in_history(claims)),
        ("no_zero_duration_dupes", no_zero_duration_dupes(claims)),
        ("no_future_dates", no_future_dates(claims, now_year)),
        ("has_dated_career", has_dated_career(claims)),
    ]
    failures = tuple((name, detail) for name, (ok, detail) in checks if not ok)
    n = len(checks)
    score = round(100 * (n - len(failures)) / n)
    p0 = any(name == "no_future_dates" for name, _ in failures)
    return CoherenceReport(score=score, failures=failures, p0=p0)
