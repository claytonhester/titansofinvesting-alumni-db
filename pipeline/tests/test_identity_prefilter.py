"""Unit tests for the deterministic identity pre-filter — pure logic, no API.

Covers the slam-dunk auto-accept rule (name + company + one secondary anchor),
its conservatism (weak/partial signals fall through to Sonnet, nothing is ever
pre-rejected), token-level company/school matching across corporate suffixes and
word order, and the full-skip case where every source is decided."""
from __future__ import annotations

import pytest

from discovery import Source
from enrichment_store import DECISION_ACCEPT
from identity import AUTO_ACCEPT, PersonAnchors
from identity_prefilter import (
    _all_tokens_present,
    _is_slam_dunk,
    _phrase_present,
    prefilter,
)

_ANCHORS = PersonAnchors(
    full_name="Jane Doe",
    company="Acme Capital",
    city="Austin",
    school="Rice University",
    titan_class=12,
)


def _src(url: str, text: str) -> Source:
    return Source(url=url, title="", description="", markdown=text, relevance=0.5)


@pytest.mark.unit
def test_phrase_present_is_word_boundary_safe() -> None:
    assert _phrase_present("jane doe", "we met jane doe today")
    assert not _phrase_present("jane doe", "janedoexample")
    assert not _phrase_present("austin", "this is exhausting")


@pytest.mark.unit
def test_all_tokens_present_ignores_suffixes_and_order() -> None:
    # "Acme Capital" matches "Capital, Acme LLC" — order and suffixes stripped.
    assert _all_tokens_present("Acme Capital", _norm("Capital, Acme LLC"))
    assert not _all_tokens_present("Acme Capital", _norm("Beta Capital"))


def _norm(text: str) -> str:
    from identity_prefilter import _normalize

    return _normalize(text)


@pytest.mark.unit
def test_slam_dunk_requires_name_company_and_secondary() -> None:
    assert _is_slam_dunk(["name", "company", "city"])
    assert _is_slam_dunk(["name", "company", "school"])
    assert not _is_slam_dunk(["name", "company"])  # no secondary anchor
    assert not _is_slam_dunk(["name", "city", "school"])  # no company
    assert not _is_slam_dunk(["company", "city", "school"])  # no name


@pytest.mark.unit
def test_strong_match_is_auto_accepted_without_sonnet() -> None:
    source = _src(
        "https://acme.com/team",
        "Jane Doe is a partner at Acme Capital in Austin. She studied at Rice University.",
    )
    out = prefilter(_ANCHORS, (source,))
    assert out.ambiguous == ()
    assert len(out.decided) == 1
    v = out.decided[0]
    assert v.decision == DECISION_ACCEPT
    assert v.confidence >= AUTO_ACCEPT
    assert v.source_url == "https://acme.com/team"
    assert "name" in v.reason and "company" in v.reason


@pytest.mark.unit
def test_name_only_falls_through_to_sonnet() -> None:
    source = _src("https://news.com/x", "Jane Doe won an award last night.")
    out = prefilter(_ANCHORS, (source,))
    assert out.decided == ()
    assert [s.url for s in out.ambiguous] == ["https://news.com/x"]


@pytest.mark.unit
def test_name_plus_company_only_is_not_a_slam_dunk() -> None:
    # Two anchors is not enough — namesake risk; Sonnet must judge.
    source = _src("https://x.com", "Jane Doe joined Acme Capital this year.")
    out = prefilter(_ANCHORS, (source,))
    assert out.decided == ()
    assert len(out.ambiguous) == 1


@pytest.mark.unit
def test_prefilter_never_rejects() -> None:
    # A clearly-unrelated page is sent to Sonnet, not dropped.
    source = _src("https://random.com", "An article about marine biology in Norway.")
    out = prefilter(_ANCHORS, (source,))
    assert out.decided == ()
    assert len(out.ambiguous) == 1


@pytest.mark.unit
def test_mixed_batch_splits_decided_and_ambiguous() -> None:
    strong = _src(
        "https://acme.com",
        "Jane Doe, Acme Capital, based in Austin; Rice University alum.",
    )
    weak = _src("https://blog.com", "A post mentioning Jane Doe in passing.")
    out = prefilter(_ANCHORS, (strong, weak))
    assert [v.source_url for v in out.decided] == ["https://acme.com"]
    assert [s.url for s in out.ambiguous] == ["https://blog.com"]


@pytest.mark.unit
def test_empty_sources_is_safe() -> None:
    out = prefilter(_ANCHORS, ())
    assert out.decided == ()
    assert out.ambiguous == ()
