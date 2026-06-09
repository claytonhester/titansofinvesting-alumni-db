"""Unit tests for career_analysis parsing + first-post-grad-employer logic."""
from __future__ import annotations

from career_analysis import (
    first_post_grad_employer,
    num_employers,
    parse_career_entry,
    tenure_years,
    years_to_md,
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


def test_parse_comma_format_from_reconciler():
    """The reconciler emits 'Title, Company (years)' — comma-separated, no ' at '."""
    e = parse_career_entry("Director of Investments, Texas A&M Foundation (2020-2025)")
    assert e.title == "Director of Investments"
    assert e.company == "Texas A&M Foundation"
    assert e.start_year == 2020 and e.end_year == 2025


def test_parse_comma_suffix_guard_is_company_only():
    """A comma before a corporate suffix is inside the company name, not a
    title/company separator."""
    e = parse_career_entry("Heritage Asset Advisors Ltd., LLP (2010-present)")
    assert e.title == ""
    assert e.company == "Heritage Asset Advisors Ltd., LLP"
    assert e.start_year == 2010 and e.end_year is None


def test_parse_company_from_quote_when_value_has_no_separator():
    """A bare title with no ' at '/comma falls back to the quote's '@ COMPANY'."""
    e = parse_career_entry(
        "Director of Investments",
        "2020 - 2025 Director of Investments @ texas a&m foundation",
    )
    assert e.company == "texas a&m foundation"
    assert e.start_year == 2020 and e.end_year == 2025


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


def test_num_employers_distinct():
    claims = [
        _career("Analyst at Goldman (2010-2014)"),
        _career("VP at Goldman (2014-2016)"),  # same firm string -> deduped
        _career("MD at Citadel (2016-present)"),
    ]
    assert num_employers(claims) == 2


def test_years_to_md_from_grad():
    claims = [
        _career("Analyst at Goldman (2010-2014)"),
        _career("Managing Director at Citadel (2018-present)"),
    ]
    assert years_to_md(claims, grad_year=2010) == 8


def test_years_to_md_none_without_md_or_grad():
    claims = [_career("Analyst at Goldman (2010-2014)")]
    assert years_to_md(claims, grad_year=2010) is None      # no MD role
    md = [_career("Managing Director at X (2018-present)")]
    assert years_to_md(md, grad_year=None) is None          # no grad year


def test_years_to_md_clamped_nonnegative():
    claims = [_career("Partner at X (2008-present)")]  # 'partner' is MD+ tier
    assert years_to_md(claims, grad_year=2010) == 0   # senior before grad -> 0


def test_tenure_years():
    assert tenure_years(2018, 2026) == 8
    assert tenure_years(None, 2026) is None
    assert tenure_years(2030, 2026) == 0  # clamped
