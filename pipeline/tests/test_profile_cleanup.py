"""Unit tests for the deterministic post-reconcile profile cleanup."""
from __future__ import annotations

from enrichment_store import ClaimRow
from profile_cleanup import (
    clean_current_title,
    clean_profile,
    dedupe_current_role,
    drop_nonprofessional_careers,
    is_nonprofessional_career,
)


def _c(claim_type, value, quote="", confidence=0.8):
    return ClaimRow(claim_type, value, "", quote, confidence, "pdl")


# --- non-professional career filter ------------------------------------------

def test_flags_student_program():
    assert is_nonprofessional_career("Titans - Group #3 at Titans of Investing (2007-2024)")
    assert is_nonprofessional_career("Tanner Fund at Texas A&M University - Mays Business School")


def test_flags_personal_site_and_volunteer():
    assert is_nonprofessional_career("Founder of UltimateDBZ.com")
    assert is_nonprofessional_career("Financial Advisory Board Member, Restoring Justice (2019-2024)")


def test_keeps_real_jobs():
    assert not is_nonprofessional_career("Managing Director at Citadel (2018-present)")
    assert not is_nonprofessional_career("Analyst at Goldman Sachs (2010-2014)")


def test_drop_nonprofessional_keeps_other_types():
    claims = [
        _c("career_history", "Analyst at Goldman (2010-2014)"),
        _c("career_history", "Titans - Group #3 at Titans of Investing (2007-2024)"),
        _c("education", "BBA from Texas A&M - Mays Business School"),  # education untouched
    ]
    out = drop_nonprofessional_careers(claims)
    assert len(out) == 2
    assert any(c.claim_type == "education" for c in out)  # school kept in education
    assert all("Titans of Investing" not in c.value for c in out)


# --- single current role ------------------------------------------------------

def test_dedupe_current_employer_prefers_anchor_company():
    """Two current employers; the one matching the most-current (open-ended) career
    entry wins, even if the other has equal confidence."""
    claims = [
        _c("current_employer", "Enverus", confidence=0.8),
        _c("current_employer", "Sonar", confidence=0.8),
        _c("career_history", "Manager at Sonar (2020-present)"),
        _c("career_history", "Analyst at Enverus (2014-2020)"),
    ]
    out = dedupe_current_role(claims)
    emps = [c.value for c in out if c.claim_type == "current_employer"]
    assert emps == ["Sonar"]


def test_dedupe_current_title_keeps_highest_confidence():
    claims = [
        _c("current_title", "Director", confidence=0.9),
        _c("current_title", "Real Assets Investment Manager", confidence=0.7),
        _c("current_employer", "TRS", confidence=0.9),
    ]
    out = dedupe_current_role(claims)
    titles = [c.value for c in out if c.claim_type == "current_title"]
    assert titles == ["Director"]


def test_dedupe_noop_when_already_single():
    claims = [_c("current_employer", "Acme"), _c("current_title", "Partner")]
    assert dedupe_current_role(claims) == claims


# --- employer-as-title --------------------------------------------------------

def test_drops_employer_as_title():
    claims = [_c("current_employer", "U.S. Army"), _c("current_title", "U.S. Army")]
    out = clean_current_title(claims)
    assert not any(c.claim_type == "current_title" for c in out)
    assert any(c.claim_type == "current_employer" for c in out)


def test_keeps_real_title():
    claims = [_c("current_employer", "Acme Capital"), _c("current_title", "Partner")]
    assert clean_current_title(claims) == claims


# --- full pipeline ------------------------------------------------------------

def test_clean_profile_composes_all_three():
    claims = [
        _c("current_employer", "Sonar", confidence=0.8),
        _c("current_employer", "Enverus", confidence=0.8),
        _c("current_title", "Sonar", confidence=0.6),          # employer-as-title
        _c("career_history", "Manager at Sonar (2020-present)"),
        _c("career_history", "Titans - Group #3 at Titans of Investing (2007-2024)"),
    ]
    out = clean_profile(claims)
    assert [c.value for c in out if c.claim_type == "current_employer"] == ["Sonar"]
    assert not any(c.claim_type == "current_title" for c in out)  # "Sonar" == employer
    assert not any("Titans of Investing" in c.value for c in out)


# --- never-news host choke point ----------------------------------------------

def test_drops_news_mention_from_broker_echo_host():
    """Regression: a wwana.com scraper page became a news_mention claim for
    Ricardo Lopez via a discovery path that forgot to filter. clean_profile is
    the final choke point — broker/records hosts never survive to persistence."""
    wwana = ClaimRow(
        "news_mention",
        "Ricardo Lopez Profile - Worldwide Association of Notable Alumni",
        "https://www.wwana.com/home/4831484-ricardo-lopez/profile?skxiu=4831484",
        "", 0.6, "sonar_press",
    )
    salary = ClaimRow(
        "news_mention", "Jane Doe salary record",
        "https://govsalaries.com/jane-doe", "", 0.6, "firecrawl_news",
    )
    real = ClaimRow(
        "news_mention", "2024-01-01 — Acme names Jane Doe CEO",
        "https://www.bizjournals.com/acme-ceo", "", 0.6, "firecrawl_news",
    )
    out = clean_profile([wwana, salary, real])
    mentions = [c for c in out if c.claim_type == "news_mention"]
    assert mentions == [real]


def test_non_news_host_only_affects_news_mentions():
    """A public_links claim to a directory host is the identity gate's business,
    not this filter's — only news_mention claims are dropped here."""
    link = ClaimRow(
        "public_links", "Profile", "https://www.zoominfo.com/p/jane", "", 0.5, "pdl",
    )
    assert link in clean_profile([link])
