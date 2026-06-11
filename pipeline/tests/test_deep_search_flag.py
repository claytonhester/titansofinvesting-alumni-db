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
    assert needs is True and "thin/undated career" in reason


def test_undated_career_flags():
    needs, reason = should_flag_for_deep_search(_bd(dated_career_share=0.5))
    assert needs is True and "thin/undated career" in reason


def test_no_bio_flags():
    needs, reason = should_flag_for_deep_search(_bd(has_bio=False))
    assert needs is True and "no bio" in reason


def test_low_score_flags():
    # A profile can have a full role+careers+bio but a <60 score via missing
    # education/press/linkedin — the score floor still flags it.
    needs, reason = should_flag_for_deep_search(_bd(score=55))
    assert needs is True and "completeness<60" in reason


def test_multiple_reasons_joined():
    needs, reason = should_flag_for_deep_search(
        _bd(score=30, has_current_role=False, career_entries=0, has_bio=False))
    assert needs is True
    for part in ("no current role", "thin/undated career", "no bio", "completeness<60"):
        assert part in reason


def test_deterministic():
    bd = _bd(score=58, has_current_role=False, career_entries=1, dated_career_share=0.0)
    assert should_flag_for_deep_search(bd) == should_flag_for_deep_search(bd)
