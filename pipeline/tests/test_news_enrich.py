"""Unit tests for the Firecrawl news extractor's source gating."""
from __future__ import annotations

from discovery import NewsDiscoveryResult, Source
from news_enrich import extract_news_mentions


class _ExplodingAnthropic:
    """Fails the test if any model call is attempted."""

    class _Messages:
        def create(self, **_):
            raise AssertionError("model must not be called for blocked hosts")

    def __init__(self):
        self.messages = self._Messages()


def _source(url: str) -> Source:
    return Source(url=url, title="t", description="d", markdown="body", relevance=0.9)


def test_broker_echo_source_skipped_before_model_call():
    """Regression for the wwana.com leak: this path created news_mention claims
    with no host filter. Blocked hosts must be skipped BEFORE the Claude call —
    zero claims and zero tokens."""
    disc = NewsDiscoveryResult(
        sources=(
            _source("https://www.wwana.com/home/123-ricardo-lopez/profile"),
            _source("https://govsalaries.com/jane-doe"),
        ),
        credits_spent=0,
    )
    result = extract_news_mentions(_ExplodingAnthropic(), "Ricardo Lopez", "JP Morgan", disc)
    assert result.claim_rows == ()
    assert result.input_tokens == 0 and result.output_tokens == 0


def test_empty_sources_no_call():
    disc = NewsDiscoveryResult(sources=(), credits_spent=0)
    result = extract_news_mentions(_ExplodingAnthropic(), "Jane", "Acme", disc)
    assert result.claim_rows == ()
