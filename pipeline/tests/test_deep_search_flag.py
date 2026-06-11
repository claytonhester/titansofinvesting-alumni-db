"""Tests for the deterministic deep-search flag rule."""
from __future__ import annotations

from compute_completeness import CompletenessBreakdown
from deep_search_flag import should_flag_for_deep_search


def _bd(score=100, has_current_role=True, has_education=True, career_entries=3,
        has_bio=True, has_press=True, has_linkedin=True, dated_career_share=1.0):
    return CompletenessBreakdown(
        score=score, has_current_role=has_current_role, has_education=has_education,
        career_entries=career_entries, has_bio=has_bio, has_press=has_press,
        has_linkedin=has_linkedin, dated_career_share=dated_career_share,
    )


def test_rich_profile_not_flagged():
    needs, reason = should_flag_for_deep_search(_bd())
    assert needs is False and reason == ""


def test_no_current_role_flags():
    needs, reason = should_flag_for_deep_search(_bd(has_current_role=False))
    assert needs is True and "no current role" in reason


def test_thin_career_flags():
    needs, reason = should_flag_for_deep_search(_bd(career_entries=2))
    assert needs is True and "thin career" in reason


def test_undated_career_does_NOT_flag():
    """A dated-share gap is data hygiene, not a deep-read target — don't flag."""
    needs, reason = should_flag_for_deep_search(_bd(dated_career_share=0.5))
    assert needs is False and reason == ""


def test_no_bio_does_NOT_flag():
    """Bio is synthesized free in the base pass — never worth a Firecrawl read."""
    needs, reason = should_flag_for_deep_search(_bd(has_bio=False))
    assert needs is False and reason == ""


def test_low_score_alone_does_NOT_flag():
    """A <60 score from missing education/press/linkedin (career intact) is not a
    deep-read target — only thin career / no current role are."""
    needs, reason = should_flag_for_deep_search(
        _bd(score=45, has_education=False, has_press=False, has_linkedin=False))
    assert needs is False and reason == ""


def test_multiple_reasons_joined():
    needs, reason = should_flag_for_deep_search(
        _bd(has_current_role=False, career_entries=0))
    assert needs is True
    for part in ("no current role", "thin career"):
        assert part in reason


def test_deterministic():
    bd = _bd(has_current_role=False, career_entries=1)
    assert should_flag_for_deep_search(bd) == should_flag_for_deep_search(bd)
