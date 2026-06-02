"""Unit tests for the identity merge gate — pure logic, no API calls.

Covers the decision thresholds, lenient model-reply parsing (fences, prose
wrapping, malformed items), the safe rejected-by-default for un-scored sources,
and the accepted_sources filter that feeds structuring.
"""
from __future__ import annotations

import pytest

from discovery import Source
from enrichment_store import DECISION_ACCEPT, DECISION_REJECT, DECISION_REVIEW
from identity import (
    AUTO_ACCEPT,
    REVIEW_FLOOR,
    accepted_sources,
    _decide,
    _parse_scores,
    _verdict_for,
)


def _src(url: str) -> Source:
    return Source(url=url, title="t", description="d", markdown="body", relevance=0.5)


@pytest.mark.unit
@pytest.mark.parametrize(
    "conf,expected",
    [
        (1.0, DECISION_ACCEPT),
        (AUTO_ACCEPT, DECISION_ACCEPT),
        (AUTO_ACCEPT - 0.01, DECISION_REVIEW),
        (REVIEW_FLOOR, DECISION_REVIEW),
        (REVIEW_FLOOR - 0.01, DECISION_REJECT),
        (0.0, DECISION_REJECT),
    ],
)
def test_decide_thresholds(conf: float, expected: str) -> None:
    assert _decide(conf) == expected


@pytest.mark.unit
def test_parse_scores_plain_array() -> None:
    text = '[{"source_url": "https://a.com", "confidence": 0.9, "reason": "same employer"}]'
    out = _parse_scores(text)
    assert out == {"https://a.com": (0.9, "same employer")}


@pytest.mark.unit
def test_parse_scores_strips_code_fence() -> None:
    text = '```json\n[{"source_url": "https://a.com", "confidence": 0.5, "reason": "ok"}]\n```'
    assert _parse_scores(text)["https://a.com"] == (0.5, "ok")


@pytest.mark.unit
def test_parse_scores_recovers_from_surrounding_prose() -> None:
    text = 'Here are my judgements: [{"source_url": "https://a.com", "confidence": 0.7, "reason": "x"}] done.'
    assert _parse_scores(text)["https://a.com"] == (0.7, "x")


@pytest.mark.unit
def test_parse_scores_malformed_returns_empty() -> None:
    assert _parse_scores("not json at all") == {}


@pytest.mark.unit
def test_parse_scores_skips_bad_items_but_keeps_good() -> None:
    text = (
        '[{"confidence": 0.9},'  # no url -> skipped
        ' "a string",'  # not an object -> skipped
        ' {"source_url": "https://b.com", "confidence": "bad", "reason": 5}]'
    )
    out = _parse_scores(text)
    assert out == {"https://b.com": (0.0, "")}  # bad confidence -> 0.0, non-str reason -> ""


@pytest.mark.unit
def test_verdict_defaults_to_reject_when_unscored() -> None:
    verdict = _verdict_for(_src("https://x.com"), {})
    assert verdict.decision == DECISION_REJECT
    assert verdict.confidence == 0.0
    assert "default" in verdict.reason.lower()


@pytest.mark.unit
def test_verdict_clamps_out_of_range_confidence() -> None:
    verdict = _verdict_for(_src("https://x.com"), {"https://x.com": (1.7, "r")})
    assert verdict.confidence == 1.0
    assert verdict.decision == DECISION_ACCEPT


@pytest.mark.unit
def test_accepted_sources_keeps_only_accepted() -> None:
    sources = (_src("https://a.com"), _src("https://b.com"), _src("https://c.com"))
    scored = {
        "https://a.com": (0.95, "match"),
        "https://b.com": (0.6, "maybe"),
        "https://c.com": (0.1, "no"),
    }
    verdicts = tuple(_verdict_for(s, scored) for s in sources)
    kept = accepted_sources(sources, verdicts)
    assert [s.url for s in kept] == ["https://a.com"]
