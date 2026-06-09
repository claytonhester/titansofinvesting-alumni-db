"""Fetch article text and focus it around a person's name for news verification.

The news curator judges items from a headline + a short snippet, which is too thin
to tell an award's HONOREE from someone merely name-dropped inside the honoree's
blurb (the "Ross Willmann named to Forty Under Forty" bug — actually Chris Halaska's
entry, which mentioned Ross). This module pulls the real article and returns a
window around where the person is named, so the verification LLM can confirm
attribution and extract the exact achievement.

Never raises: a scrape failure or a missing name yields "" / a head-of-article
window, so the caller degrades to its snippet-only judgment instead of aborting.
"""
from __future__ import annotations

import re
from typing import Callable

# A scrape returns one credit's worth of markdown; we only need the slice around
# the person, so keep the window tight to bound prompt tokens.
DEFAULT_RADIUS = 900


def make_firecrawl_fetcher(firecrawl) -> Callable[[str], str]:
    """A url->markdown fetcher backed by Firecrawl. Never raises; "" on failure."""

    def fetch(url: str) -> str:
        if not url:
            return ""
        try:
            doc = firecrawl.scrape(url, formats=["markdown"])
        except Exception:
            return ""
        md = getattr(doc, "markdown", None)
        if md is None and isinstance(doc, dict):
            md = doc.get("markdown")
        return md or ""

    return fetch


def _name_variants(name: str) -> list[str]:
    """Search terms for locating the person in the article, most-specific first."""
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    variants: list[str] = []
    if len(parts) >= 2:
        variants.append(" ".join(parts).lower())   # full name
        variants.append(parts[-1].lower())          # last name
    elif parts:
        variants.append(parts[0].lower())
    return [v for v in variants if len(v) > 1]


# Always include the article HEAD so the verifier can see WHOSE page/profile this
# is — the difference between "this person is the honoree" and "this person is
# name-dropped inside another honoree's profile" (an award page titled '40 Under
# 40' that is actually one other person's Q&A naming our target).
HEAD_CHARS = 450
# Recognition / move signals — the award sentence often sits well away from the
# name (e.g. Forbes lists the honor BELOW the bio), so we always include a window
# around the first such keyword too, or it gets cut off and the page reads as a
# generic profile.
_SIGNAL_KW = (
    "under 30", "under 40", "30u30", "40u40", "named to", "named one",
    "award", "honor", "honour", "honoree", "recipient", "recogni",
    "ranked", "ranking", "the list", "list of", "promoted", "appointed",
    "elected", "winner", "fellow",
)


def _first_index(lowered: str, needles: tuple[str, ...]) -> int:
    for n in needles:
        i = lowered.find(n)
        if i != -1:
            return i
    return -1


def _slice(text: str, center: int, radius: int) -> tuple[int, int]:
    return max(0, center - radius), min(len(text), center + radius)


def name_window(text: str, name: str, radius: int = DEFAULT_RADIUS) -> str:
    """Focused context for verification: the article HEAD (who the page is about)
    plus a window around the person's name plus a window around the first
    recognition signal (award/ranking/move), merged in document order and capped.
    This shows the verifier both the page's true subject and the achievement
    sentence even when they sit far apart. Falls back to the head when the name is
    absent. Never raises; "" on empty input."""
    if not text:
        return ""
    lowered = text.lower()
    spans: list[tuple[int, int]] = [(0, min(len(text), HEAD_CHARS))]

    name_idx = _first_index(lowered, tuple(_name_variants(name)))
    if name_idx == -1:
        # Name not found: the head plus the first signal is the best we can show.
        sig = _first_index(lowered, _SIGNAL_KW)
        if sig != -1:
            spans.append(_slice(text, sig, radius // 2))
    else:
        spans.append(_slice(text, name_idx, radius))
        sig = _first_index(lowered, _SIGNAL_KW)
        if sig != -1 and not (name_idx - radius <= sig <= name_idx + radius):
            spans.append(_slice(text, sig, radius // 2))

    # Merge overlapping/adjacent spans in document order.
    spans.sort()
    merged: list[list[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])

    parts = [text[s:e].strip() for s, e in merged]
    out = "\n[...]\n".join(p for p in parts if p)
    return out[:2600]
