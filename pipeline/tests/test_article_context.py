"""Unit tests for article_context — name-window extraction (no network)."""
from __future__ import annotations

import pytest

from article_context import name_window


@pytest.mark.unit
def test_window_includes_the_name_mention() -> None:
    text = ("A " * 500) + "Spotlight on Jane Doe, a partner. " + ("B " * 500)
    win = name_window(text, "Jane Doe", radius=80)
    assert "Jane Doe" in win


@pytest.mark.unit
def test_window_includes_head_so_subject_is_visible() -> None:
    # The page is Chris Halaska's profile; our target is named deep inside it. The
    # head must ride along so the verifier sees whose page it is.
    text = "2016 Forty Under Forty. Chris Halaska, CIO. " + ("filler. " * 300) + \
        "My dream team includes Ross Willmann and others."
    win = name_window(text, "Ross Willmann", radius=120)
    assert "Chris Halaska" in win       # head present
    assert "Ross Willmann" in win       # name window present
    assert "[...]" in win               # they are stitched, not contiguous


@pytest.mark.unit
def test_window_includes_recognition_signal_far_from_name() -> None:
    # Name at the very top; the award sentence sits far below (the Forbes case).
    text = "Nicholas Gagnet. Investor, Coatue. " + ("bio. " * 300) + \
        "Forbes Lists: 30 Under 30 - Finance 2026."
    win = name_window(text, "Nicholas Gagnet", radius=150)
    assert "Nicholas Gagnet" in win
    assert "30 Under 30" in win          # signal window pulled in despite distance


@pytest.mark.unit
def test_window_falls_back_to_last_name() -> None:
    text = ("x " * 300) + "Mr. Willmann was mentioned here." + ("y " * 300)
    win = name_window(text, "Ross Willmann", radius=40)
    assert "Willmann" in win


@pytest.mark.unit
def test_empty_text_is_safe() -> None:
    assert name_window("", "Jane Doe") == ""
