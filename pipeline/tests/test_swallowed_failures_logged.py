"""Swallowed failures must leave at least one warning line: the never-raises
adapters still degrade, but no longer silently."""
from __future__ import annotations

import logging

import pytest

import phase2_enrich
from cost_log import remaining_credits
from discovery import _search_with_retry
from enrichment_store import ClaimRow
from linkedin_firecrawl import fetch_linkedin
from reconcile import reconcile_claims


class _Boom:
    """Any method call raises — stands in for a Firecrawl/Anthropic client."""

    def __init__(self):
        self.messages = self

    def __getattr__(self, name):
        def _raise(*a, **k):
            raise RuntimeError(f"simulated {name} outage")
        return _raise


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.unit
def test_cost_meter_failure_is_logged(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert remaining_credits(_Boom()) is None
    assert any("credit meter" in m for m in _warnings(caplog))


@pytest.mark.unit
def test_reconcile_failure_is_logged_and_returns_claims(caplog) -> None:
    claims = [
        ClaimRow("career_history", "Analyst at A (2010-2012)", "u", "", 0.8, "x"),
        ClaimRow("career_history", "Analyst, A", "u2", "", 0.7, "pdl"),
    ]
    with caplog.at_level(logging.WARNING):
        out, tin, tout = reconcile_claims(_Boom(), "Jane Doe", claims)
    assert out == claims and (tin, tout) == (0, 0)
    assert any("reconcile" in m and "Jane Doe" in m for m in _warnings(caplog))


@pytest.mark.unit
def test_search_retry_exhaustion_is_logged(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        assert _search_with_retry(_Boom(), "q", 5, backoff_base=0.0, attempts=2) == []
    assert any("gave up" in m for m in _warnings(caplog))


@pytest.mark.unit
def test_linkedin_agent_failure_is_logged(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        res = fetch_linkedin(_Boom(), "Jane Doe", employer="Acme", city="Austin")
    assert res.found is False and res.credits_used == 0
    assert any("linkedin agent" in m and "Jane Doe" in m for m in _warnings(caplog))


@pytest.mark.unit
def test_seed_search_outage_is_logged(monkeypatch, caplog) -> None:
    def _down(*a, **k):
        raise RuntimeError("network down")
    monkeypatch.setattr(phase2_enrich, "search_linkedin_candidates", _down)
    person = phase2_enrich.Person(1, "Jane Doe", "Acme", "Austin", "Texas A&M", 2)
    with caplog.at_level(logging.WARNING):
        url, claim = phase2_enrich._resolve_linkedin_seed(object(), "key", person, [])
    assert url == "" and claim is None
    assert any("linkedin search failed" in m for m in _warnings(caplog))
