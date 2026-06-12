"""Unit tests for the cross-industry seniority ladder (seniority_v2) and the
per-person fold (reclassify_levels.compute_person_levels). Pure functions only —
no Haiku, no DB."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reclassify_levels import compute_person_levels
from seniority_v2 import (
    LEVELS,
    classify_level_keyword,
    level_index,
)

FINANCE = "Goldman Sachs Capital Partners"
CORP = "Google"


# --- the whole point: VP / Director invert by sector ---------------------------

def test_finance_vp_is_manager_not_senior():
    assert classify_level_keyword("Vice President", FINANCE) == "Manager"
    assert classify_level_keyword("Principal", FINANCE) == "Manager"


def test_finance_director_and_md_are_senior():
    assert classify_level_keyword("Director", FINANCE) == "Senior Leadership"
    assert classify_level_keyword("Managing Director", FINANCE) == "Senior Leadership"
    assert classify_level_keyword("Head of Fintech Banking", FINANCE) == "Senior Leadership"


def test_corporate_vp_is_senior_director_is_manager():
    assert classify_level_keyword("Vice President", CORP) == "Senior Leadership"
    assert classify_level_keyword("SVP, Growth", CORP) == "Senior Leadership"
    assert classify_level_keyword("Director of Sales", CORP) == "Manager"
    assert classify_level_keyword("Senior Director", CORP) == "Manager"


# --- universal rungs -----------------------------------------------------------

def test_entry_rung():
    assert classify_level_keyword("Analyst", FINANCE) == "Entry / IC"
    assert classify_level_keyword("Senior Associate", FINANCE) == "Entry / IC"
    assert classify_level_keyword("Investment Banking Summer Analyst", FINANCE) == "Entry / IC"


def test_executive_and_founder():
    assert classify_level_keyword("Chief Financial Officer", CORP) == "Executive / Founder"
    assert classify_level_keyword("CEO and Co-founder", CORP) == "Executive / Founder"
    assert classify_level_keyword("Managing Partner", FINANCE) == "Executive / Founder"
    assert classify_level_keyword("President", CORP) == "Executive / Founder"


def test_plain_partner_is_senior_leadership():
    assert classify_level_keyword("Partner", "Vinson & Elkins") == "Senior Leadership"


def test_manager_family():
    assert classify_level_keyword("Senior Manager, Monetization", CORP) == "Manager"
    assert classify_level_keyword("Portfolio Manager", FINANCE) == "Manager"
    assert classify_level_keyword("Manager at Deloitte", "Deloitte") == "Manager"


def test_non_title_sink():
    assert classify_level_keyword("JPM Alternatives", "JPMorgan") == "Non-title"
    assert classify_level_keyword("Private Markets", FINANCE) == "Non-title"
    assert classify_level_keyword("Assistant Lacrosse Coach", "UT") == "Non-title"
    assert classify_level_keyword("MBA Candidate", "UT") == "Non-title"


def test_rankless_real_role_defaults_to_entry_not_senior():
    # Conservative: never award Manager+ without evidence.
    assert classify_level_keyword("Investor", FINANCE) == "Entry / IC"
    assert classify_level_keyword("Investment Professional", FINANCE) == "Entry / IC"


def test_level_index_ordering_and_non_title():
    assert level_index("Entry / IC") == 0
    assert level_index("Manager") == 1
    assert level_index("Senior Leadership") == 2
    assert level_index("Executive / Founder") == 3
    assert level_index("Non-title") is None  # dropped from the spine


# --- per-person fold -----------------------------------------------------------

def _lm(*pairs):
    """Build a label_map from (title, employer, level) triples."""
    return {(t.lower(), e.lower()): lvl for t, e, lvl in pairs}


def test_years_to_senior_from_grad():
    roles = [("Analyst", "GS", 2010), ("Managing Director", "Citadel", 2018)]
    lm = _lm(("Analyst", "GS", "Entry / IC"),
             ("Managing Director", "Citadel", "Senior Leadership"))
    pl = compute_person_levels(roles, grad_year=2010, label_map=lm)
    assert pl.peak_level == "Senior Leadership"
    assert pl.reached_senior is True
    assert pl.reached_manager is True
    assert pl.years_to_senior == 8


def test_manager_threshold_lower_than_senior():
    # Corporate Director = Manager rung, never reaches Senior Leadership.
    roles = [("Analyst", "Acme", 2012), ("Director of Sales", "Acme", 2017)]
    lm = _lm(("Analyst", "Acme", "Entry / IC"),
             ("Director of Sales", "Acme", "Manager"))
    pl = compute_person_levels(roles, grad_year=2012, label_map=lm)
    assert pl.reached_manager is True
    assert pl.reached_senior is False
    assert pl.years_to_manager == 5
    assert pl.years_to_senior is None


def test_non_title_dropped_from_spine():
    roles = [("JPM Alternatives", "JPM", 2015)]
    lm = _lm(("JPM Alternatives", "JPM", "Non-title"))
    pl = compute_person_levels(roles, grad_year=2010, label_map=lm)
    assert pl.peak_level == ""
    assert pl.reached_manager is False
    assert pl.years_to_senior is None


def test_years_clamped_nonnegative():
    roles = [("Partner", "X", 2008)]
    lm = _lm(("Partner", "X", "Senior Leadership"))
    pl = compute_person_levels(roles, grad_year=2010, label_map=lm)
    assert pl.years_to_senior == 0  # senior before grad -> clamp


def test_no_grad_year_yields_none_velocity_but_keeps_threshold():
    roles = [("Managing Director", "X", 2018)]
    lm = _lm(("Managing Director", "X", "Senior Leadership"))
    pl = compute_person_levels(roles, grad_year=None, label_map=lm)
    assert pl.reached_senior is True
    assert pl.years_to_senior is None
