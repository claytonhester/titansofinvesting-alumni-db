"""Tests for the Haiku sector classifier (insights_llm.classify_sectors).

Uses a fake Anthropic client so no network/billing happens. Verifies: off-list
or missing labels fall back to the deterministic classifier; valid labels are
kept; empty input is a zero-cost no-op; labels are aligned to input order.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from insights_llm import classify_sectors


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
        self.usage = _Usage(100, 20)


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


def test_empty_items_is_zero_cost_noop() -> None:
    client = _FakeClient({})
    result = classify_sectors(client, [])
    assert result.labels == ()
    assert result.input_tokens == 0
    assert client.messages.calls == 0


def test_valid_labels_are_kept_in_order() -> None:
    client = _FakeClient({"0": "Law / Legal", "1": "Technology"})
    items = [("Smith LLP", "Partner", ""), ("Acme", "Engineer", "")]
    result = classify_sectors(client, items)
    assert result.labels == ("Law / Legal", "Technology")
    assert result.input_tokens == 100


def test_offlist_label_falls_back_to_deterministic() -> None:
    # Model returns a garbage label for item 0 -> fall back to classify_sector,
    # which uses the industry ("real estate") to place it correctly.
    client = _FakeClient({"0": "Made Up Sector", "1": "Healthcare & Life Sciences"})
    items = [("Anon Co", "Agent", "real estate"), ("Mercy Hospital", "Nurse", "")]
    result = classify_sectors(client, items)
    assert result.labels == ("Real Estate", "Healthcare & Life Sciences")


def test_missing_index_falls_back_to_deterministic() -> None:
    # Model omits item 1 entirely -> deterministic fallback on its (company, industry).
    client = _FakeClient({"0": "Technology"})
    items = [("Acme Software", "Eng", "computer software"), ("Goldman Sachs", "Analyst", "")]
    result = classify_sectors(client, items)
    assert result.labels == ("Technology", "Investment Banking")


def test_unparseable_response_degrades_entirely_to_fallback() -> None:
    # Response isn't JSON at all -> every item uses the deterministic fallback.
    client = _FakeClient({})  # empty object -> no keys -> all fall back
    items = [("Kirkland & Ellis LLP", "Associate", ""), ("Joe's Diner", "Owner", "")]
    result = classify_sectors(client, items)
    assert result.labels == ("Law / Legal", "Other / Operating")
