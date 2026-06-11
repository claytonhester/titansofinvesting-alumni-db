"""Pure-factory tests for the deterministic Coherence rules.

No mocks — coherence.py is reference-free and LLM-free, so every test builds a
ClaimRow list by hand and asserts the rule + the aggregated report. Mirrors the
factory style of test_career_analysis / test_qa_audit.
"""
from __future__ import annotations

from coherence import (
    coherence_report,
    current_employer_in_history,
    exactly_one_current_role,
    has_dated_career,
    no_future_dates,
    no_zero_duration_dupes,
)
from enrichment_store import ClaimRow

_NOW = 2026


def _emp(name: str) -> ClaimRow:
    return ClaimRow("current_employer", name, "", "", 0.9, "x")


def _title(name: str) -> ClaimRow:
    return ClaimRow("current_title", name, "", "", 0.9, "x")


def _career(value: str) -> ClaimRow:
    return ClaimRow("career_history", value, "", "", 0.9, "x")


def _edu(value: str = "BBA from A&M") -> ClaimRow:
    return ClaimRow("education", value, "", "", 0.9, "x")


# --- exactly_one_current_role ---------------------------------------------------

def test_one_current_role_passes_with_single():
    ok, _ = exactly_one_current_role([_emp("Acme"), _title("CEO")])
    assert ok


def test_one_current_role_fails_with_two_employers():
    ok, detail = exactly_one_current_role([_emp("Acme"), _emp("Globex")])
    assert not ok and "current employers" in detail


def test_one_current_role_fails_with_two_titles():
    ok, detail = exactly_one_current_role([_title("CEO"), _title("Partner")])
    assert not ok and "current titles" in detail


# --- current_employer_in_history -----------------------------------------------

def test_employer_in_history_matches_open_ended():
    claims = [_emp("Acme Capital"), _career("Partner at Acme Capital (2018-present)")]
    ok, _ = current_employer_in_history(claims)
    assert ok


def test_employer_in_history_flags_mismatch():
    claims = [
        _emp("Acme Capital"),
        _career("Partner at Globex (2020-present)"),
    ]
    ok, detail = current_employer_in_history(claims)
    assert not ok and "Acme Capital" in detail


def test_employer_in_history_uses_latest_when_no_open_ended():
    # Most recent dated role is Acme (2022); the current employer should match it.
    claims = [
        _emp("Acme"),
        _career("Analyst at Bank (2018-2020)"),
        _career("VP at Acme (2020-2022)"),
    ]
    ok, _ = current_employer_in_history(claims)
    assert ok


def test_employer_in_history_noop_without_history():
    ok, _ = current_employer_in_history([_emp("Acme")])
    assert ok


def test_employer_in_history_allows_concurrent_roles():
    # The Will Carpenter case: current employer (TRS) is one of several concurrent
    # open-ended roles. A later-started board/adjunct role must NOT flag it.
    claims = [
        _emp("Teacher Retirement System of Texas"),
        _career("Director at Teacher Retirement System of Texas (2020-present)"),
        _career("Senior Fellow at Council on Foreign Relations (2022-present)"),
        _career("Adjunct Professor at UT Austin (2017-present)"),
    ]
    ok, _ = current_employer_in_history(claims)
    assert ok


def test_employer_in_history_flags_current_absent_from_active_roles():
    # Current employer is in NONE of the active roles -> genuinely incoherent.
    claims = [
        _emp("Mystery Corp"),
        _career("Director at Acme (2020-present)"),
        _career("Advisor at Globex (2021-present)"),
    ]
    ok, detail = current_employer_in_history(claims)
    assert not ok and "not among active roles" in detail


# --- suffix-tolerant employer match --------------------------------------------

def test_employer_in_history_tolerates_corp_suffix():
    # 'Lenox Park Solutions, Inc.' (current) vs 'Lenox Park Solutions' (history)
    # is the same company — must NOT flag.
    claims = [
        _emp("Lenox Park Solutions, Inc."),
        _career("Founder at Lenox Park Solutions (2014-present)"),
    ]
    ok, _ = current_employer_in_history(claims)
    assert ok


# --- no_zero_duration_dupes ----------------------------------------------------

def test_zero_duration_dup_is_flagged():
    claims = [
        _career("Partner at Acme (2018-present)"),
        _career("Partner at Acme (2018-2018)"),
    ]
    ok, detail = no_zero_duration_dupes(claims)
    assert not ok and "zero-duration" in detail


def test_zero_duration_standalone_is_fine():
    # A genuine single-year stint with no longer sibling at the same employer.
    claims = [_career("Fellow at Org (2018-2018)")]
    ok, _ = no_zero_duration_dupes(claims)
    assert ok


# --- no_future_dates -----------------------------------------------------------

def test_future_date_is_flagged():
    claims = [_career("VP at Acme (2030-present)")]
    ok, detail = no_future_dates(claims, _NOW)
    assert not ok and "future date" in detail


def test_no_future_date_passes():
    claims = [_career("VP at Acme (2020-present)")]
    ok, _ = no_future_dates(claims, _NOW)
    assert ok


# --- has_dated_career ----------------------------------------------------------

def test_undated_career_is_flagged():
    # The Bart Howe failure: titles, no years.
    claims = [_career("Managing Director at Acme"), _career("Analyst at Bank")]
    ok, detail = has_dated_career(claims)
    assert not ok and "undated" in detail


def test_dated_career_passes():
    ok, _ = has_dated_career([_career("VP at Acme (2020-present)")])
    assert ok


def test_no_career_is_not_a_coherence_failure():
    ok, _ = has_dated_career([_emp("Acme"), _edu()])
    assert ok


# --- coherence_report aggregation ----------------------------------------------

def _clean_person() -> list[ClaimRow]:
    return [
        _emp("Acme Capital"),
        _title("Partner"),
        _edu(),
        _career("Partner at Acme Capital (2018-present)"),
        _career("VP at Bank (2015-2018)"),
    ]


def test_report_clean_person_scores_100():
    rep = coherence_report(_clean_person(), grad_year=2014, now_year=_NOW)
    assert rep.score == 100 and not rep.failures and not rep.p0


def test_report_single_failure_drops_score():
    claims = _clean_person() + [_emp("Globex")]  # two current employers
    rep = coherence_report(claims, grad_year=2014, now_year=_NOW)
    assert rep.score == round(100 * 4 / 5)  # 5 rules, one failed
    assert any(name == "one_current_role" for name, _ in rep.failures)
    assert not rep.p0


def test_report_future_date_sets_p0():
    claims = [_emp("Acme"), _career("VP at Acme (2030-present)")]
    rep = coherence_report(claims, grad_year=2010, now_year=_NOW)
    assert rep.p0
    assert any(name == "no_future_dates" for name, _ in rep.failures)


def test_report_empty_claims_scores_100():
    # No claims => every rule is a no-op pass; an empty profile isn't *incoherent*.
    rep = coherence_report([], grad_year=None, now_year=_NOW)
    assert rep.score == 100 and not rep.p0
