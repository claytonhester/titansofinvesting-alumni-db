"""Unit tests for the person↔company link store + the no-spend career backfill."""
from __future__ import annotations

import sqlite3

import pytest

from backfill_person_company import _parse_career, _subset_match
from person_company_store import (
    PersonCompany,
    init_person_company_schema,
    linked_domains,
    replace_person_companies,
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_person_company_schema(c)
    return c


@pytest.mark.unit
def test_replace_and_linked_domains_skip_empty() -> None:
    c = _conn()
    replace_person_companies(c, 1, [
        PersonCompany(1, "sageadvisory.com", "Sage Advisory", "Partner", 2019, None, True, "pdl"),
        PersonCompany(1, "", "Old Bank", "Analyst", 2013, 2015, False, "pdl"),  # no domain -> skip
    ])
    assert linked_domains(c) == {"sageadvisory.com"}


@pytest.mark.unit
def test_replace_is_idempotent_per_person() -> None:
    c = _conn()
    replace_person_companies(c, 1, [PersonCompany(1, "a.com", "A", "", None, None, True)])
    replace_person_companies(c, 1, [PersonCompany(1, "b.com", "B", "", None, None, True)])
    assert linked_domains(c) == {"b.com"}  # old rows replaced


@pytest.mark.unit
def test_parse_career_extracts_title_company_years() -> None:
    assert _parse_career("Analyst at Citi (2015-2017)") == ("Analyst", "Citi", 2015, 2017, False)
    t, co, s, e, cur = _parse_career("Financial Advisor at Warwick (2020 - Present)")
    assert co == "Warwick" and s == 2020 and cur is True and e is None
    # no ' at ' -> whole thing is the company
    assert _parse_career("Sage Advisory")[1] == "Sage Advisory"


@pytest.mark.unit
def test_subset_match_links_known_firms_only() -> None:
    companies = [("trs.texas.gov", "Teacher Retirement System of Texas"),
                 ("sageadvisory.com", "Sage Advisory"), ("bpc.com", "Brighton Park Capital")]
    # subset match (geo token 'texas' excluded, distinctive tokens align)
    assert _subset_match("Teacher Retirement System of Texas", companies) == "trs.texas.gov"
    assert _subset_match("Sage Advisory Services", companies) == "sageadvisory.com"
    # acronym fallback
    assert _subset_match("Brighton Park Capital", companies) == "bpc.com"
    # unrelated firm -> no false link
    assert _subset_match("Goldman Sachs", companies) == ""


@pytest.mark.unit
def test_subset_match_avoids_single_shared_token_false_positive() -> None:
    companies = [("sageadvisory.com", "Sage Advisory")]
    # "Sage Therapeutics" shares only 'sage' but is not a subset -> no match.
    assert _subset_match("Sage Therapeutics", companies) == ""


@pytest.mark.unit
def test_subset_match_rejects_proper_subset_on_a_single_token() -> None:
    # "Lincoln International" collapses to {'lincoln'} ('international' is a generic
    # stopword). "Lincoln Financial Group" -> {'lincoln','financial'}. A PROPER
    # subset on the lone surname 'lincoln' is NOT enough: these are different firms
    # (an M&A advisor vs an insurer). Live failure: Karn Nopany mis-attributed.
    companies = [("lincolninternational.com", "Lincoln International")]
    assert _subset_match("Lincoln Financial Group", companies) == ""


@pytest.mark.unit
def test_subset_match_keeps_exact_single_token_match() -> None:
    # The fix must not over-correct: the genuine Lincoln International employee
    # ("Lincoln International LLC" -> {'lincoln'}) still matches by EXACT equality.
    companies = [("lincolninternational.com", "Lincoln International")]
    assert _subset_match("Lincoln International LLC", companies) == "lincolninternational.com"


@pytest.mark.unit
def test_subset_match_rejects_two_char_acronym_collision() -> None:
    # "Shift Admin" -> "sa"; "Sage Advisory" -> "sa". A 2-letter acronym collision
    # must not link unrelated firms. Live failure: Brock Birkenfeld (COO of Shift
    # Admin) mis-attributed to Sage Advisory's company page.
    companies = [("sageadvisory.com", "Sage Advisory")]
    assert _subset_match("Shift Admin", companies) == ""
