"""Unit tests for the sifting report's pure classifiers."""
from __future__ import annotations

import pytest

from enrich_report import Bucket, classify_links
from enrichment_store import ClaimRow


def _link(value, url):
    return ClaimRow("public_links", value, url, "", 0.8, "perplexity")


@pytest.mark.unit
def test_classify_links_keeps_genuine_appearance() -> None:
    claims = [_link("Podcast: ETFs in model portfolios", "https://www.etf.com/podcasts/x")]
    bucket = classify_links(claims, "Jane Doe")
    assert bucket.kept == ["Podcast: ETFs in model portfolios"]
    assert bucket.dropped == []


@pytest.mark.unit
def test_classify_links_drops_broker_social_and_bio() -> None:
    claims = [
        _link("LinkedIn", "https://linkedin.com/in/jane"),
        _link("Jane Doe profile", "https://www.zoominfo.com/p/Jane/1"),
        _link("Meet Our Team", "https://firm.com/team"),
        _link("Jane Doe, CFA", "https://firm.com/jane"),
        _link("Real interview on rates", "https://www.barrons.com/x"),
    ]
    bucket = classify_links(claims, "Jane Doe")
    assert bucket.kept == ["Real interview on rates"]
    reasons = {item: reason for item, reason in bucket.dropped}
    assert "LinkedIn" in reasons and "linkedin" in reasons["LinkedIn"].lower()
    assert any("broker" in r or "directory" in r for r in reasons.values())
    assert any("boilerplate" in r for r in reasons.values())
    assert any("bio" in r or "name-only" in r for r in reasons.values())


@pytest.mark.unit
def test_classify_links_dedupes() -> None:
    claims = [
        _link("Interview", "https://www.barrons.com/x"),
        _link("Interview (dup)", "https://www.barrons.com/x/"),
    ]
    bucket = classify_links(claims, "Jane Doe")
    assert len(bucket.kept) == 1


@pytest.mark.unit
def test_bucket_is_immutable_dataclass() -> None:
    b = Bucket(kept=["a"], dropped=[("b", "why")])
    with pytest.raises(Exception):
        b.kept = []  # type: ignore[misc]
