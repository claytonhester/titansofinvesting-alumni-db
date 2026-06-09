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
from news_store import NEWS_CATEGORIES, CuratedNews
from structuring import HAIKU_MODEL

# The feed shows only PERSON-centric categories — "Company News" (about the firm,
# not the person) is excluded entirely, per the editorial bar.
_FEED_CATEGORIES = tuple(c for c in NEWS_CATEGORIES if c != "Company News")
# Hard cap per person so one media-active alumnus can't dominate the feed.
MAX_NEWS_PER_PERSON = 3

_DATE_SEP = " — "  # matches web/lib/news.ts NEWS_DATE_SEP

# Press hits land in TWO claim types: `news_mention` (Firecrawl press pass, dated)
# and `public_links` (the Perplexity mention pass + PDL profiles). public_links is
# overloaded — it also holds social profiles and people-directory aggregator pages,
# which are NOT news. We curate news_mention plus the press-worthy public_links,
# dropping links whose host is a social network or a directory aggregator.
_SOCIAL_HOSTS = frozenset({
    "linkedin.com", "twitter.com", "x.com", "facebook.com", "instagram.com",
    "youtube.com", "youtu.be", "klout.com", "foursquare.com", "pinterest.com",
    "tiktok.com", "threads.net", "reddit.com", "medium.com",
})
_DIRECTORY_HOSTS = frozenset({
    "theorg.com", "advisorcheck.com", "indyfin.com", "getwarmer.com",
    "app.getwarmer.com", "crunchbase.com", "zoominfo.com", "rocketreach.co",
    "signalhire.com", "wsj.com/market-data", "bloomberg.com/profile",
    "pitchbook.com", "zoomgov.com", "spokeo.com",
})


# An item only earns a slot in the feed if the model says show=True AND it clears
# this importance bar. The feed is meant to be SCARCE — most people have nothing
# genuinely newsworthy, and that's fine. Tune up to be stricter.
NEWS_MIN_IMPORTANCE = 0.5

# Title patterns for firm boilerplate, directory listings, and filings — pages that
# are NOT "about the person doing something", just their name on a corporate/profile
# page. Dropped before the model so they never reach the feed (and don't cost tokens).
_BOILERPLATE_TITLE_KW = (
    "meet our team", "our team", "meet the team", "team members", "our people",
    "leadership team", "management team", "company overview", "about us",
    "about the firm", "company profile", "firm overview", "our firm", "culture",
    "careers", "join our team", "staff directory", "employee directory",
    "form adv", "brochure supplement", "part 2b", "prospectus", "fact sheet",
    "annual report", "press kit", "media kit", "[pdf]", "contact us",
    "privacy policy", "terms of service",
)


def _is_boilerplate_title(title: str) -> bool:
    t = (title or "").lower()
    return any(kw in t for kw in _BOILERPLATE_TITLE_KW)


def _is_press_worthy_link(host: str) -> bool:
    """A public_links host counts as a press mention only if it's neither a social
    network nor a people-directory aggregator (those are profiles, not news)."""
    if not host:
        return False
    return host not in _SOCIAL_HOSTS and host not in _DIRECTORY_HOSTS


def news_items(claims: list[ClaimRow]) -> list[ClaimRow]:
    """The claims to curate: every news_mention, plus public_links whose host is a
    genuine content source (not a social profile or directory listing)."""
    items: list[ClaimRow] = []
    for c in claims:
        if _is_boilerplate_title(c.value):
            continue  # firm/profile boilerplate is never news
        if c.claim_type == "news_mention":
            items.append(c)
        elif c.claim_type == "public_links" and _is_press_worthy_link(_host(c.source_url)):
            items.append(c)
    return items


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


_SYSTEM = """You are a SELECTIVE financial-news editor. Your job is to keep the \
feed scarce and high-signal: show ONLY items that are genuinely about THIS person \
— what they personally said, did, were recognized for, or a career move they made. \
Most candidates should be cut. It is completely fine to keep nothing.

For each candidate (headline + snippet), decide four things:

1. show — boolean. TRUE only if the item is ABOUT THIS PERSON specifically and \
worth a reader's attention: their interview/podcast/commentary, an award or \
ranking naming them, their hire/promotion/new role, a deal THEY led or are quoted \
on. Set FALSE for:
   - news about their COMPANY that isn't about them (product launches, the firm's \
deals, "what the company is doing")
   - profile/directory/listing pages, "team" pages, bios, regulatory filings
   - passing name-drops, generic PR, anything where they are not the subject
   When in doubt, set show=false.
   IMPORTANT: if the person is INDIVIDUALLY named, quoted, or featured, the item is \
about them even when the headline leads with their employer or an agency (e.g. \
"Highest-paid employees at X" that names them, or "X firm's CIO says ..."). Judge \
by whether THIS person is singled out, not by what the headline starts with.
2. category — choose EXACTLY ONE, copied verbatim:
   - "Funding & Deals" (a deal THEY led / are quoted on)
   - "Leadership Moves" (THEIR hire, promotion, new role, board seat, departure)
   - "Market Views" (THEIR commentary, outlook, interview, podcast)
   - "Recognition" (an award/ranking/honor naming THEM — including being \
individually named in a notable list: highest-paid, top performers, power lists, \
40-under-40, etc.)
   - "Company News" (about the firm with the person NOT individually featured — \
pair with show=false)
3. summary — ONE plain sentence (max ~25 words), leading with the PERSON, on what \
they did or said. No hype, no preamble.
4. importance — float 0.0-1.0: how notable this is (a fund close, a major \
promotion, a named ranking is high; a routine quote is mid; anything you'd set \
show=false on is low).

Return ONLY a JSON array, one object per candidate, SAME order:
[{"index": <int>, "show": <bool>, "category": "<one of the five>", \
"summary": "<one line>", "importance": <float>}]"""


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
    """Curate a person's press mentions. Returns (curated, haiku_in, haiku_out).
    Reads news_mention claims AND press-worthy public_links (see news_items), so
    Perplexity-discovered articles/podcasts surface, not just the Firecrawl press
    pass. Every article yields a row (model verdict, or a deterministic fallback).
    Returns ([], 0, 0) when there are no mentions."""
    items = news_items(mentions)
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
        verdict = parsed.get(i)
        # No model judgment, or the editor said don't show -> drop. The feed is
        # deliberately scarce; an item earns its slot, it isn't shown by default.
        if not verdict or not bool(verdict.get("show")):
            continue
        category = verdict.get("category")
        # Only person-centric categories make the feed; "Company News" (about the
        # firm, not the person) and any unrecognized label are dropped.
        if category not in _FEED_CATEGORIES:
            continue
        importance = _clamp_importance(verdict.get("importance"), 0.0)
        if importance < NEWS_MIN_IMPORTANCE:
            continue
        summary = str(verdict.get("summary") or "").strip() or claim.quote or headline
        curated.append(CuratedNews(
            headline=headline,
            summary=summary,
            category=category,
            date=date,
            source_url=claim.source_url,
            source_host=_host(claim.source_url),
            importance=importance,
        ))
    # Best first, and cap per person so one media-active alumnus can't flood the feed.
    curated.sort(key=lambda c: c.importance, reverse=True)
    return curated[:MAX_NEWS_PER_PERSON], tok_in, tok_out
