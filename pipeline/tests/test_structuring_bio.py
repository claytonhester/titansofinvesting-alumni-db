"""Tests for profile_from_claims — the bridge that lets bio synthesis compose
from the full reconciled claim set (PDL included), not just Firecrawl extraction.
"""
from __future__ import annotations

from enrichment_store import ClaimRow
from structuring import profile_from_claims


def _c(ct, value, conf=0.8):
    return ClaimRow(claim_type=ct, value=value, source_url="", quote="",
                    confidence=conf, extraction_method="pdl")


def test_singles_take_first_lists_accumulate():
    claims = [
        _c("current_title", "CEO"),
        _c("current_employer", "Acme"),
        _c("location", "Austin"),
        _c("career_history", "Analyst at TRS"),
        _c("career_history", "Partner at Acme"),
        _c("education", "BBA from A&M"),
    ]
    prof = profile_from_claims(claims)
    assert prof["current_title"] == {"value": "CEO", "confidence": 0.8}
    assert len(prof["career_history"]) == 2
    assert [e["value"] for e in prof["education"]] == ["BBA from A&M"]


def test_public_links_and_mentions_excluded():
    prof = profile_from_claims([
        _c("public_links", "LinkedIn"),
        _c("news_mention", "Some article"),
        _c("current_employer", "Acme"),
    ])
    assert "public_links" not in prof and "news_mention" not in prof
    assert prof["current_employer"]["value"] == "Acme"


def test_single_value_first_seen_wins():
    prof = profile_from_claims([
        _c("current_employer", "Paragon Intel"),
        _c("current_employer", "Old Co"),
    ])
    assert prof["current_employer"]["value"] == "Paragon Intel"


def test_empty_claims_yields_empty_profile():
    assert profile_from_claims([]) == {}
