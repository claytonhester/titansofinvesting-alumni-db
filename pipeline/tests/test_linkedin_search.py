"""Pure tests for the search-based LinkedIn finder — scoring + URL choice.

No network: LinkedInCandidate lists are built by hand. Mirrors the pilot cases
that motivated it (Paul-Marc's wrong PDL url, Lauren's namesakes, Annie's tie).
"""
from __future__ import annotations

from linkedin_search import (
    LinkedInCandidate,
    _normalize,
    choose_linkedin_url,
)


def _c(slug, score, evidence="name,employer"):
    return LinkedInCandidate(f"https://linkedin.com/in/{slug}", score, evidence, "search")


# --- normalization -------------------------------------------------------------

def test_normalize_strips_and_lowercases():
    assert _normalize("https://www.LinkedIn.com/in/Jane-Doe/") == \
        "https://linkedin.com/in/jane-doe"


def test_normalize_from_snippet_text():
    assert _normalize("see linkedin.com/in/pmschweitzer for more") == \
        "https://linkedin.com/in/pmschweitzer"


def test_normalize_no_match_is_empty():
    assert _normalize("https://example.com/x") == ""


# --- choose_linkedin_url -------------------------------------------------------

def test_no_candidates_uses_pdl():
    url, why = choose_linkedin_url("https://linkedin.com/in/will-x", [])
    assert url == "https://linkedin.com/in/will-x" and "pdl-only" in why


def test_pdl_confirmed_when_search_agrees():
    cands = [_c("travis-l-crawford", 3.0)]
    url, why = choose_linkedin_url("https://linkedin.com/in/travis-l-crawford", cands)
    assert url.endswith("/travis-l-crawford") and "confirmed" in why


def test_strong_search_overrides_wrong_pdl():
    # The Paul-Marc case: PDL guessed a different slug; a search hit naming the
    # person + employer overrides it.
    cands = [_c("pmschweitzer", 2.0)]
    url, why = choose_linkedin_url("https://linkedin.com/in/paul-marc-schweitzer", cands)
    assert url.endswith("/pmschweitzer") and "overrides" in why


def test_weak_search_keeps_pdl():
    # The Lauren case: namesakes with no employer match (score < 2) don't override.
    cands = [_c("lauren-sloan-2321", 1.5, "name,slug"),
             _c("lauren-e-sloan-5a9", 1.5, "name,slug")]
    url, why = choose_linkedin_url("https://linkedin.com/in/lroperinc", cands)
    assert url.endswith("/lroperinc") and "fallback" in why


def test_ambiguous_tie_keeps_pdl():
    # The Annie case: 3 equally-corroborated profiles -> don't guess, keep PDL.
    cands = [_c("annie-stewart-51b", 3.0), _c("annie-stewart-084", 3.0),
             _c("annie-stewart-173", 3.0)]
    url, why = choose_linkedin_url("https://linkedin.com/in/annieelizabethstewart", cands)
    assert url.endswith("/annieelizabethstewart") and "ambiguous" in why


def test_strong_single_search_with_no_pdl():
    url, why = choose_linkedin_url("", [_c("jane-doe", 2.5)])
    assert url.endswith("/jane-doe") and "search" in why


def test_ambiguous_tie_no_pdl_makes_no_pick():
    url, why = choose_linkedin_url("", [_c("a-1", 3.0), _c("a-2", 3.0)])
    assert url == "" and "ambiguous" in why
