"""Unit tests for sonar_news — all HTTP mocked, no spend.

Covers what keeps the Sonar press source honest:
1. A clean press item becomes a dated news_mention claim the curator can read.
2. The is_about_this_person gate drops namesake hits before they reach the feed.
3. Aggregator/data-broker domains are dropped even when Sonar vouches for them.
4. Cost prefers the authoritative usage.cost, else prices tokens + a request fee.
5. Missing key / empty name short-circuit free; any failure degrades to empty.
"""
from __future__ import annotations

import json

import httpx
import pytest

from sonar_news import (
    CLAIM_TYPE,
    EXTRACTION_METHOD,
    discover_press_sonar,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _body(press: list[dict], *, cost: float | None = None, tokens: tuple[int, int] | None = None) -> dict:
    usage: dict = {}
    if cost is not None:
        usage["cost"] = {"total_cost": cost}
    if tokens is not None:
        usage["prompt_tokens"], usage["completion_tokens"] = tokens
    return {
        "choices": [{"message": {"content": json.dumps({"items": press})}}],
        "usage": usage,
    }


def _ok(body: dict):
    return lambda req: httpx.Response(200, json=body)


@pytest.mark.unit
def test_clean_item_becomes_dated_news_mention() -> None:
    press = [{
        "headline": "Jane Doe Named to Forty Under Forty",
        "url": "https://www.businessjournal.com/40under40/jane-doe",
        "date": "2025-09-15",
        "why": "Recognized for leading the firm's credit strategy.",
        "is_about_this_person": True,
    }]
    result = discover_press_sonar(
        _client(_ok(_body(press, cost=0.0083))),
        "Jane Doe", "Apex Capital", "Austin", perplexity_key="key", facets=("x",),
    )
    assert result.found == 1 and result.kept == 1 and result.requests == 1
    assert result.cost_usd == pytest.approx(0.0083)
    (row,) = result.claim_rows
    assert row.claim_type == CLAIM_TYPE
    assert row.extraction_method == EXTRACTION_METHOD
    # value carries the ISO date so the curator's _split_value renders the year.
    assert row.value == "2025-09-15 — Jane Doe Named to Forty Under Forty"
    assert row.quote == "Recognized for leading the firm's credit strategy."


@pytest.mark.unit
def test_namesake_item_is_dropped() -> None:
    """is_about_this_person=false means Sonar can't tell it apart — never emit."""
    press = [{
        "headline": "Jane Doe wins city marathon",
        "url": "https://www.runnersworld.com/jane",
        "date": "2025-01-01",
        "why": "A different Jane Doe.",
        "is_about_this_person": False,
    }]
    result = discover_press_sonar(
        _client(_ok(_body(press))), "Jane Doe", "Apex", "Austin",
        perplexity_key="key", facets=("x",),
    )
    assert result.found == 1 and result.kept == 0 and result.claim_rows == ()


@pytest.mark.unit
def test_aggregator_domain_dropped_even_if_vouched() -> None:
    press = [{
        "headline": "Jane Doe profile",
        "url": "https://www.zoominfo.com/p/Jane-Doe/123",
        "date": "",
        "why": "Directory page.",
        "is_about_this_person": True,
    }]
    result = discover_press_sonar(
        _client(_ok(_body(press))), "Jane Doe", "Apex", "Austin",
        perplexity_key="key", facets=("x",),
    )
    assert result.found == 1 and result.kept == 0


@pytest.mark.unit
def test_broker_echo_domain_dropped_even_if_vouched() -> None:
    """Regression (Ricardo Lopez, person 779): wwana.com — an SEO scraper directory
    the identity gate already rejected — slipped through Sonar as a news_mention
    because the news path kept its own drifted domain list. Every broker/echo host
    in directory_hosts.NON_NEWS_HOSTS must be dropped here, vouched or not."""
    press = [{
        "headline": "Ricardo Lopez — Worldwide Association of Notable Alumni",
        "url": "https://www.wwana.com/profile/ricardo-lopez",
        "date": "2026-06-01",
        "why": "Directory page echoing the queried name.",
        "is_about_this_person": True,
    }]
    result = discover_press_sonar(
        _client(_ok(_body(press))), "Ricardo Lopez", "Apex", "Austin",
        perplexity_key="key", facets=("x",),
    )
    assert result.found == 1 and result.kept == 0 and result.claim_rows == ()


@pytest.mark.unit
def test_undated_item_keeps_bare_headline() -> None:
    press = [{
        "headline": "Jane Doe on the macro outlook",
        "url": "https://www.barrons.com/articles/jane",
        "date": "",
        "why": "Her rates view.",
        "is_about_this_person": True,
    }]
    result = discover_press_sonar(
        _client(_ok(_body(press))), "Jane Doe", "Apex", "Austin",
        perplexity_key="key", facets=("x",),
    )
    (row,) = result.claim_rows
    assert row.value == "Jane Doe on the macro outlook"  # no date prefix


@pytest.mark.unit
def test_item_missing_url_or_headline_dropped() -> None:
    press = [
        {"headline": "", "url": "https://x.com/a", "date": "", "why": "", "is_about_this_person": True},
        {"headline": "Has no url", "url": "", "date": "", "why": "", "is_about_this_person": True},
    ]
    result = discover_press_sonar(
        _client(_ok(_body(press))), "Jane Doe", "Apex", "Austin",
        perplexity_key="key", facets=("x",),
    )
    assert result.kept == 0


@pytest.mark.unit
def test_cost_falls_back_to_token_pricing() -> None:
    """With no usage.cost reported, price the tokens + the per-request fee."""
    from sonar_news import _PRICE_IN, _PRICE_OUT, _PRICE_REQUEST

    result = discover_press_sonar(
        _client(_ok(_body([], tokens=(1_000_000, 1_000_000)))),
        "Jane Doe", "Apex", "Austin", perplexity_key="key", facets=("x",),
    )
    expected = _PRICE_IN + _PRICE_OUT + _PRICE_REQUEST
    assert result.cost_usd == pytest.approx(expected)


@pytest.mark.unit
def test_missing_key_short_circuits_without_request() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("should not issue a request without a key")

    result = discover_press_sonar(
        _client(handler), "Jane Doe", "Apex", "Austin", perplexity_key=None,
    )
    assert result.claim_rows == () and result.requests == 0 and result.cost_usd == 0.0


@pytest.mark.unit
def test_empty_name_short_circuits() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("should not issue a request for an empty name")

    result = discover_press_sonar(
        _client(handler), "   ", "Apex", "Austin", perplexity_key="key",
    )
    assert result.requests == 0


@pytest.mark.unit
def test_network_failure_degrades_to_empty() -> None:
    """A transport error must never raise — it counts the attempt at zero cost."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = discover_press_sonar(
        _client(handler), "Jane Doe", "Apex", "Austin", perplexity_key="key", facets=("x",),
    )
    assert result.claim_rows == () and result.requests == 1 and result.cost_usd == 0.0


@pytest.mark.unit
def test_malformed_content_degrades_to_empty() -> None:
    body = {"choices": [{"message": {"content": "not json at all"}}], "usage": {"cost": {"total_cost": 0.01}}}
    result = discover_press_sonar(
        _client(_ok(body)), "Jane Doe", "Apex", "Austin", perplexity_key="key", facets=("x",),
    )
    # The call happened and is priced, but no claims could be parsed.
    assert result.claim_rows == () and result.cost_usd == pytest.approx(0.01)


def _capturing(captured: list[dict]) -> httpx.Client:
    """A client that records each request payload and returns an empty result."""
    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(json.loads(req.content))
        return httpx.Response(200, json=_body([]))
    return _client(handler)


@pytest.mark.unit
def test_prompts_are_not_finance_biased() -> None:
    """The system/user prompts must NOT hardcode finance or mention the program —
    that biases search for non-finance alumni (lawyers, operators) and surfaces
    directory/LinkedIn noise. With no role/industry known, the prompt stays neutral."""
    captured: list[dict] = []
    discover_press_sonar(
        _capturing(captured), "Jane Doe", "Apex", "Austin",
        perplexity_key="key", facets=("x",),
    )
    system = captured[0]["messages"][0]["content"]
    user = captured[0]["messages"][1]["content"]
    assert "finance alumni directory" not in system
    assert "Titans of Investing" not in user
    assert "finance/investing/business" not in user
    assert "likely work in finance" not in user
    # neutral fallback when we don't know their role/field
    assert "(unknown)" in user


@pytest.mark.unit
def test_role_and_industry_injected_when_known() -> None:
    """When we DO know the person's role/field, the prompt adapts to them."""
    captured: list[dict] = []
    discover_press_sonar(
        _capturing(captured), "Jane Doe", "Vinson & Elkins", "Houston",
        perplexity_key="key", facets=("x",),
        role="Partner", industry="Legal Services",
    )
    user = captured[0]["messages"][1]["content"]
    assert "Partner" in user
    assert "Legal Services" in user


@pytest.mark.unit
def test_award_facet_is_not_an_enumerated_list() -> None:
    """The award facet asks generally for recognition — no 40u40/30u30 laundry list
    (the enumeration dragged in junk and biased toward named lists)."""
    from sonar_news import _FACETS

    award = _FACETS[0].lower()
    assert "40-under-40" not in award
    assert "30 under 30" not in award
    assert "power list" not in award
    assert "fellowship" not in award
    assert "award" in award or "recognition" in award


@pytest.mark.unit
def test_targeted_past_company_asks_added_and_capped_at_three() -> None:
    """Multi-company search: one targeted ask per real past firm, capped at 3,
    on top of the thematic facets, all pooled into one result."""
    captured: list[dict] = []
    discover_press_sonar(
        _capturing(captured), "Jane Doe", "Apex", "Austin", perplexity_key="key",
        facets=("thematic",),
        past_companies=("Goldman Sachs", "UTIMCO", "Bridgewater", "Citadel"),
    )
    assert len(captured) == 4  # 1 thematic + 3 targeted (4th company dropped by cap)
    user_texts = " ".join(c["messages"][1]["content"] for c in captured)
    assert "Goldman Sachs" in user_texts
    assert "UTIMCO" in user_texts
    assert "Bridgewater" in user_texts
    assert "Citadel" not in user_texts  # capped at 3


@pytest.mark.unit
def test_targeted_asks_skip_current_employer_and_blanks() -> None:
    """A past-company equal to the current employer (already searched by the thematic
    facets) and blank entries are skipped."""
    captured: list[dict] = []
    discover_press_sonar(
        _capturing(captured), "Jane Doe", "Apex Capital", "Austin", perplexity_key="key",
        facets=("thematic",),
        past_companies=("Apex Capital", "", "   ", "Lehman Brothers"),
    )
    assert len(captured) == 2  # 1 thematic + 1 targeted (only Lehman survives)
    user_texts = " ".join(c["messages"][1]["content"] for c in captured)
    assert "Lehman Brothers" in user_texts


@pytest.mark.unit
def test_multiple_facets_pool_and_dedupe_by_url() -> None:
    """Each facet is its own call; results pool and de-dupe by URL, cost sums."""
    item = {"headline": "Jane Doe keynote at FinForum", "url": "https://finforum.com/jane",
            "date": "", "why": "Spoke on credit.", "is_about_this_person": True}
    # Same mock body for every call → the item should appear ONCE after dedupe,
    # and cost should be summed across the facet calls.
    result = discover_press_sonar(
        _client(_ok(_body([item], cost=0.008))),
        "Jane Doe", "Apex", "Austin", perplexity_key="key",
        facets=("awards", "speaking", "deals"),
    )
    assert result.requests == 3
    assert result.cost_usd == pytest.approx(0.024)   # 3 × 0.008
    assert result.found == 1 and result.kept == 1     # deduped to one URL
    assert len(result.claim_rows) == 1
