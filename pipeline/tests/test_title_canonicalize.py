"""Tests for title cleanup + canonicalization.

- clean_title_basic: free, deterministic suffix-stripping (the fallback).
- canonicalize_titles: the Haiku fold (fake client; no network), verifying
  near-duplicate titles merge, counts sum, omitted titles fall back to the
  deterministic cleaner, and a bad response degrades to the fallback for all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from insights_llm import canonicalize_titles
from insights_rollup import clean_title_basic, order_titles_by_seniority
from insights_store import TitleCount


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Assistant Professor, Department of Pathology", "Assistant Professor"),
        (
            "AI Governance and Agentic AI Sales Leader (Subject Matter Expert) - IBM UKI Data Platforms",
            "AI Governance and Agentic AI Sales Leader (Subject Matter Expert)",
        ),
        ("Associate - Private Equity", "Associate"),
        ("Managing Director at Goldman Sachs", "Managing Director"),
        ("Partner", "Partner"),  # already clean
        ("Vice President", "Vice President"),
        ("", ""),
    ],
)
def test_clean_title_basic(raw: str, expected: str) -> None:
    assert clean_title_basic(raw) == expected


# --- Haiku canonicalizer with a fake client ------------------------------

@dataclass
class _Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class _Block:
    text: str
    type: str = "text"


class _Resp:
    def __init__(self, payload: dict) -> None:
        self.content = [_Block(json.dumps(payload))]
        self.usage = _Usage(120, 30)


class _FakeMessages:
    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    def create(self, **_kwargs):
        self.calls += 1
        return _Resp(self._payload)


class _FakeClient:
    def __init__(self, payload: dict) -> None:
        self.messages = _FakeMessages(payload)


def test_empty_is_zero_cost_noop() -> None:
    client = _FakeClient({})
    result = canonicalize_titles(client, [])
    assert result.titles == ()
    assert client.messages.calls == 0


def test_near_duplicates_merge_and_counts_sum() -> None:
    client = _FakeClient(
        {
            "Associate": "Associate",
            "Associate Attorney": "Associate",
            "Associate - Private Equity": "Associate",
            "Partner": "Partner",
        }
    )
    counts = [
        ("Associate", 1),
        ("Associate Attorney", 1),
        ("Associate - Private Equity", 1),
        ("Partner", 4),
    ]
    result = canonicalize_titles(client, counts)
    as_dict = {t.title: t.count for t in result.titles}
    assert as_dict["Associate"] == 3  # three variants folded
    assert as_dict["Partner"] == 4
    # Ordered by count desc: Partner (4) before Associate (3).
    assert result.titles[0].title == "Partner"


def test_omitted_title_falls_back_to_cleaner() -> None:
    # Model maps only "Partner"; the professor title falls back to clean_title_basic.
    client = _FakeClient({"Partner": "Partner"})
    counts = [("Partner", 2), ("Assistant Professor, Department of Pathology", 1)]
    result = canonicalize_titles(client, counts)
    labels = {t.title for t in result.titles}
    assert "Assistant Professor" in labels  # deterministic fallback applied
    assert "Partner" in labels


def test_unparseable_response_degrades_to_fallback() -> None:
    client = _FakeClient({})  # no keys -> every title uses clean_title_basic
    counts = [("Managing Director at KKR", 1), ("Associate - Credit", 1)]
    result = canonicalize_titles(client, counts)
    labels = {t.title for t in result.titles}
    assert labels == {"Managing Director", "Associate"}


# --- ordering by seniority (most senior first) ---------------------------

def test_order_titles_by_seniority_ranks_senior_first() -> None:
    titles = [
        TitleCount("Associate", 5),
        TitleCount("Chief Executive Officer", 2),
        TitleCount("Partner", 8),
        TitleCount("Vice President", 5),
        TitleCount("Managing Director", 3),
    ]
    ordered = [t.title for t in order_titles_by_seniority(titles)]
    # C-suite > Partner/Founder > Director/MD > VP > Analyst/Associate
    assert ordered == [
        "Chief Executive Officer",
        "Partner",
        "Managing Director",
        "Vice President",
        "Associate",
    ]


def test_order_within_tier_by_count_then_alpha() -> None:
    titles = [
        TitleCount("Senior Associate", 4),
        TitleCount("Associate", 5),
        TitleCount("Analyst", 5),
    ]
    ordered = [t.title for t in order_titles_by_seniority(titles)]
    # All same tier (Analyst / Associate): larger count first, then A-Z on ties.
    assert ordered == ["Analyst", "Associate", "Senior Associate"]


def test_unknown_titles_sink_to_bottom() -> None:
    titles = [
        TitleCount("Entrepreneur", 9),  # Unknown — must NOT lead despite big count
        TitleCount("Vice President", 1),
        TitleCount("Partner", 1),
    ]
    ordered = [t.title for t in order_titles_by_seniority(titles)]
    assert ordered == ["Partner", "Vice President", "Entrepreneur"]
