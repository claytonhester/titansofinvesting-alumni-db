"""Unit tests for profile_metrics: advanced-degree + Texas geography."""
from __future__ import annotations

from profile_metrics import has_advanced_degree, is_texas, left_texas


def test_advanced_degree_detected():
    assert has_advanced_degree(["MBA from Rice"]) is True
    assert has_advanced_degree(["J.D. from UT Law"]) is True
    assert has_advanced_degree(["PhD from Stanford"]) is True
    assert has_advanced_degree(["Master of Finance, LSE"]) is True


def test_undergrad_only_not_advanced():
    assert has_advanced_degree(["BBA from Texas A&M"]) is False
    assert has_advanced_degree(["BS from Baylor"]) is False
    assert has_advanced_degree([]) is False


def test_is_texas():
    assert is_texas("Austin, TX") is True
    assert is_texas("Houston, Texas") is True
    assert is_texas("College Station") is True
    assert is_texas("New York, NY") is False
    assert is_texas("") is False


def test_left_texas_tristate():
    assert left_texas("New York, NY") is True
    assert left_texas("Dallas, TX") is False
    assert left_texas("") is None        # unknown current location
    assert left_texas("   ") is None
