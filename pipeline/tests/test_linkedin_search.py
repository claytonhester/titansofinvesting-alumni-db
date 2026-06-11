"""Pure tests for the search-based LinkedIn finder — scoring + URL choice.

No network: LinkedInCandidate lists are built by hand. Mirrors the pilot cases
that motivated it (Jean-Luc's wrong PDL url, Marisa's namesakes, Nora's tie).
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
    assert _normalize("see linkedin.com/in/jlvandermeer for more") == \
        "https://linkedin.com/in/jlvandermeer"


def test_normalize_no_match_is_empty():
    assert _normalize("https://example.com/x") == ""


# --- choose_linkedin_url -------------------------------------------------------

def test_no_candidates_uses_pdl():
    url, why = choose_linkedin_url("https://linkedin.com/in/will-x", [])
    assert url == "https://linkedin.com/in/will-x" and "pdl-only" in why


def test_pdl_confirmed_when_search_agrees():
    cands = [_c("devon-l-ashworth", 3.0)]
    url, why = choose_linkedin_url("https://linkedin.com/in/devon-l-ashworth", cands)
    assert url.endswith("/devon-l-ashworth") and "confirmed" in why


def test_strong_search_overrides_wrong_pdl():
    # The Jean-Luc case: PDL guessed a different slug; a search hit naming the
    # person + employer overrides it.
    cands = [_c("jlvandermeer", 2.0)]
    url, why = choose_linkedin_url("https://linkedin.com/in/jean-luc-vandermeer", cands)
    assert url.endswith("/jlvandermeer") and "overrides" in why


def test_weak_search_keeps_pdl():
    # The Marisa case: namesakes with no employer match (score < 2) don't override.
    cands = [_c("marisa-oyelaran-2321", 1.5, "name,slug"),
             _c("marisa-e-oyelaran-5a9", 1.5, "name,slug")]
    url, why = choose_linkedin_url("https://linkedin.com/in/moyelaraninc", cands)
    assert url.endswith("/moyelaraninc") and "fallback" in why


def test_ambiguous_tie_keeps_pdl():
    # The Nora case: 3 equally-corroborated profiles -> don't guess, keep PDL.
    cands = [_c("annie-stewart-51b", 3.0), _c("annie-stewart-084", 3.0),
             _c("annie-stewart-173", 3.0)]
    url, why = choose_linkedin_url("https://linkedin.com/in/noraelizabethwhitfield", cands)
    assert url.endswith("/noraelizabethwhitfield") and "ambiguous" in why


def test_strong_single_search_with_no_pdl():
    url, why = choose_linkedin_url("", [_c("jane-doe", 2.5)])
    assert url.endswith("/jane-doe") and "search" in why


def test_ambiguous_tie_no_pdl_makes_no_pick():
    url, why = choose_linkedin_url("", [_c("a-1", 3.0), _c("a-2", 3.0)])
    assert url == "" and "ambiguous" in why
