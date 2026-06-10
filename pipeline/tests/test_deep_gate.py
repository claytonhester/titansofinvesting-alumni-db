"""Unit tests for deep_gate — when to spend Firecrawl, and a hard credit ceiling."""
from __future__ import annotations

import pytest

from deep_gate import FirecrawlBudget, is_high_signal


@pytest.mark.unit
def test_pdl_match_alone_is_high_signal() -> None:
    # A confident PDL match is strong identity + firmographics — worth the deep pull.
    assert is_high_signal(pdl_matched=True, trusted_count=0, has_current_employer=False)


@pytest.mark.unit
def test_two_sources_plus_employer_is_high_signal() -> None:
    assert is_high_signal(pdl_matched=False, trusted_count=2, has_current_employer=True)


@pytest.mark.unit
def test_thin_no_match_is_low_signal() -> None:
    # No PDL match, only one source, no resolved employer → not worth Firecrawl.
    assert not is_high_signal(pdl_matched=False, trusted_count=1, has_current_employer=True)
    assert not is_high_signal(pdl_matched=False, trusted_count=2, has_current_employer=False)
    assert not is_high_signal(pdl_matched=False, trusted_count=0, has_current_employer=False)


@pytest.mark.unit
def test_budget_fires_until_spent() -> None:
    b = FirecrawlBudget(30)
    assert b.decide().fire is True
    b.charge(25)
    assert b.remaining == 5
    assert b.decide().fire is True   # still some left
    b.charge(10)                     # overspend clamps to zero
    assert b.remaining == 0
    d = b.decide()
    assert d.fire is False and "budget" in d.reason.lower()


@pytest.mark.unit
def test_zero_budget_never_fires() -> None:
    b = FirecrawlBudget(0)
    assert b.decide().fire is False


@pytest.mark.unit
def test_negative_budget_clamped() -> None:
    b = FirecrawlBudget(-100)
    assert b.remaining == 0 and b.decide().fire is False
