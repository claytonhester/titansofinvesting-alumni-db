"""Haiku news-curation agent.

Raw press hits arrive as `news_mention` claims — a date, a headline, a scraped
snippet, and a source URL. This agent turns that pile into a curated feed: it
assigns each article ONE category, writes a tight one-line summary, and scores
how important/interesting the story is (so the web can lead with the best and
group the rest). One Haiku call per person over all their mentions.

Never raises: on any model/parse failure each article falls back to a neutral
category, its scraped snippet as the summary, and a mid importance — so the feed
still populates. With no client at all, the deterministic fallback is used.
"""
from __future__ import annotations

import json
from urllib.parse import urlparse

from anthropic import Anthropic

from enrichment_store import ClaimRow
from news_store import DEFAULT_CATEGORY, NEWS_CATEGORIES, CuratedNews
from structuring import HAIKU_MODEL

_DATE_SEP = " — "  # matches web/lib/news.ts NEWS_DATE_SEP


def _split_value(value: str) -> tuple[str, str]:
    """news_mention value is 'YYYY-MM-DD — Headline'; split off a leading ISO date."""
    idx = value.find(_DATE_SEP)
    if idx > 0 and _is_iso_date(value[:idx]):
        return value[:idx], value[idx + len(_DATE_SEP):]
    return "", value


def _is_iso_date(s: str) -> bool:
    return len(s) == 10 and s[4] == "-" and s[7] == "-" and s.replace("-", "").isdigit()


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname.removeprefix("www.") if url else ""
    except Exception:
        return ""


_SYSTEM = """You are a financial-news editor curating press mentions of one \
finance/investing professional (a Titans of Investing alumnus). For each article \
you are given its headline and a snippet. Do three things per article:

1. category — choose EXACTLY ONE, copied verbatim:
   - "Funding & Deals" (fund closes, raises, acquisitions, investments)
   - "Leadership Moves" (hires, promotions, new roles, board seats, departures)
   - "Market Views" (their commentary, outlook, opinion, interviews on markets)
   - "Recognition" (awards, rankings, honors, "40 under 40")
   - "Company News" (anything else about their firm or them)
2. summary — ONE plain sentence (max ~25 words) capturing what's newsworthy. No \
hype, no preamble. If the snippet is too thin, summarize the headline.
3. importance — a float 0.0-1.0: how genuinely important/interesting this is for \
a reader scanning the cohort's news (a fund close or major promotion is high; a \
passing name-drop is low).

Return ONLY a JSON array, one object per article, SAME order:
[{"index": <int>, "category": "<one of the five>", "summary": "<one line>", \
"importance": <float>}]"""


def _build_user(name: str, employer: str, articles: list[tuple[str, str]]) -> str:
    lines = [
        f"Person: {name}",
        f"Known employer: {employer or '(unknown)'}",
        "",
        "Articles:",
    ]
    for i, (headline, snippet) in enumerate(articles):
        lines.append(f"[{i}] HEADLINE: {headline}")
        if snippet:
            lines.append(f"    SNIPPET: {snippet}")
    return "\n".join(lines)


def _parse(text: str, n: int) -> dict[int, dict]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    parsed: object = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        s, e = cleaned.find("["), cleaned.rfind("]")
        if 0 <= s < e:
            try:
                parsed = json.loads(cleaned[s : e + 1])
            except json.JSONDecodeError:
                parsed = None
    out: dict[int, dict] = {}
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, dict):
                try:
                    out[int(item.get("index"))] = item
                except (TypeError, ValueError):
                    continue
    return out


def _clamp_importance(value: object, fallback: float) -> float:
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(1.0, f))


def curate_news(
    client: Anthropic | None,
    name: str,
    employer: str,
    mentions: list[ClaimRow],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 1024,
) -> tuple[list[CuratedNews], int, int]:
    """Curate a person's news_mention claims. Returns (curated, haiku_in,
    haiku_out). Every article yields a row (model verdict, or a deterministic
    fallback). Returns ([], 0, 0) when there are no mentions."""
    items = [c for c in mentions if c.claim_type == "news_mention"]
    if not items:
        return [], 0, 0

    parsed: dict[int, dict] = {}
    tok_in = tok_out = 0
    articles = [_split_value(c.value) for c in items]  # (date, headline)
    if client is not None:
        try:
            response = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": _build_user(
                    name, employer, [(h, c.quote) for (_, h), c in zip(articles, items)]
                )}],
            )
            text = "".join(b.text for b in response.content if b.type == "text")
            tok_in = response.usage.input_tokens
            tok_out = response.usage.output_tokens
            parsed = _parse(text, len(items))
        except Exception:
            parsed = {}

    curated: list[CuratedNews] = []
    for i, ((date, headline), claim) in enumerate(zip(articles, items)):
        verdict = parsed.get(i, {})
        category = verdict.get("category")
        if category not in NEWS_CATEGORIES:
            category = DEFAULT_CATEGORY
        summary = str(verdict.get("summary") or "").strip() or claim.quote or headline
        importance = _clamp_importance(verdict.get("importance"), 0.5)
        curated.append(CuratedNews(
            headline=headline,
            summary=summary,
            category=category,
            date=date,
            source_url=claim.source_url,
            source_host=_host(claim.source_url),
            importance=importance,
        ))
    return curated, tok_in, tok_out
