"""Unit tests for career_analysis parsing + first-post-grad-employer logic."""
from __future__ import annotations

from career_analysis import (
    first_post_grad_employer,
    parse_career_entry,
)
from enrichment_store import ClaimRow


def _career(value, quote=""):
    return ClaimRow("career_history", value, "", quote, 0.8, "pdl")


def test_parse_dated_role():
    e = parse_career_entry("Analyst at TRS (2015-2020)")
    assert e.title == "Analyst" and e.company == "TRS"
    assert e.start_year == 2015 and e.end_year == 2020


def test_parse_open_ended_role():
    e = parse_career_entry("Partner at Acme Capital (2020-present)")
    assert e.company == "Acme Capital"
    assert e.start_year == 2020 and e.end_year is None


def test_parse_from_quote_when_value_undated():
    e = parse_career_entry("Analyst at TRS", "2015 - 2020 Analyst @ TRS")
    assert e.start_year == 2015 and e.end_year == 2020


def test_parse_no_company():
    e = parse_career_entry("Founder")
    assert e.title == "Founder" and e.company == ""
    assert e.start_year is None


def test_parse_multiple_at_keeps_last_as_company():
    e = parse_career_entry("Head of Research at Capital at Work (2019-2022)")
    assert e.company == "Work"  # split on last ' at '


def test_first_post_grad_skips_pre_grad_internship():
    claims = [
        _career("Intern at BigBank (2013-2014)"),
        _career("Analyst at Goldman (2016-2019)"),
        _career("VP at Citadel (2019-present)"),
    ]
    assert first_post_grad_employer(claims, grad_year=2016) == "Goldman"


def test_first_post_grad_falls_back_to_earliest_when_none_after_grad():
    claims = [_career("Analyst at Goldman (2010-2014)")]
    # grad_year 2020 but only pre-2020 roles -> don't discard, return earliest
    assert first_post_grad_employer(claims, grad_year=2020) == "Goldman"


def test_first_employer_earliest_when_grad_unknown():
    claims = [
        _career("VP at Citadel (2019-present)"),
        _career("Analyst at Goldman (2015-2019)"),
    ]
    assert first_post_grad_employer(claims, grad_year=None) == "Goldman"


def test_first_employer_undated_fallback():
    claims = [_career("Analyst at Goldman")]
    assert first_post_grad_employer(claims, grad_year=None) == "Goldman"


def test_first_employer_empty_when_no_company():
    claims = [_career("Founder"), _career("Investor")]
    assert first_post_grad_employer(claims, grad_year=None) == ""


def test_no_career_claims_returns_empty():
    claims = [ClaimRow("current_title", "CEO", "", "", 0.9, "pdl")]
    assert first_post_grad_employer(claims, grad_year=2010) == ""
