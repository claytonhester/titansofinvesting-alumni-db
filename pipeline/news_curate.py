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
import re
from typing import Callable
from urllib.parse import urlparse

from anthropic import Anthropic

from article_context import name_window
from directory_hosts import (
    DIRECTORY_HOSTS,
    PUBLIC_RECORDS_HOSTS,
    SOCIAL_HOSTS,
    registrable_host,
)
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
# Social + directory/broker hosts share the core defined in directory_hosts.py;
# news keeps a couple of path-scoped market-data entries as local extras.
_SOCIAL_HOSTS = SOCIAL_HOSTS
_DIRECTORY_HOSTS = DIRECTORY_HOSTS | frozenset({
    "wsj.com/market-data", "bloomberg.com/profile",
})
# Public-records / salary databases — dropped from the feed regardless of the
# model's verdict (see directory_hosts.py). Live case: a TRS "Highest Paid State
# Employees" row mis-shown as Recognition.
_PUBLIC_RECORDS_HOSTS = PUBLIC_RECORDS_HOSTS


# How the article relates to THIS person. The feed shows only the two depths where
# the person is the story; the rest are noise. Ordered weakest→strongest so the rank
# weights below read naturally.
#   not_about   — the firm/a namesake/a generic page; the person isn't the subject
#   passing     — named in passing; the mention carries no insight about them
#   substantive — individually named with a real, describable point about them
#                 (a quote with a view, a ranking that singles them out, a role move)
#   feature     — the article is substantially about them (profile, interview, Q&A)
_SHOWN_DEPTHS = frozenset({"substantive", "feature"})
# Feature outranks substantive at equal importance, so a profile leads over a quote.
_DEPTH_RANK = {"feature": 1.0, "substantive": 0.0}

# A shown item must also clear this importance bar. The feed is meant to be SCARCE —
# most people have nothing genuinely newsworthy, and that's fine. Tune up to be stricter.
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


# Professional credentials / generation suffixes that may trail a name on a bio
# page ("Ross Willmann, CFA") without making it an article.
_CREDENTIAL_TOKENS = frozenset({
    "cfa", "cpa", "cfp", "caia", "frm", "mba", "phd", "jd", "cma", "caia",
    "jr", "sr", "ii", "iii", "iv", "md", "esq", "cef", "chfc", "clu",
})


def _is_name_only_title(title: str, name: str) -> bool:
    """A headline that is JUST the person's name (optionally with credentials) is a
    bio / profile / team-page heading, never an article — e.g. 'Ross Willmann, CFA'
    on his own firm's site. A real article adds words beyond the name. Dropped so
    these never reach the feed even when a source returns them as 'press'."""
    name_tokens = set(re.findall(r"[a-z]+", name.lower()))
    if not name_tokens:
        return False
    head_tokens = re.findall(r"[a-z]+", title.lower())
    if not head_tokens:
        return False
    extra = [t for t in head_tokens if t not in name_tokens and t not in _CREDENTIAL_TOKENS]
    return not extra  # nothing beyond the person's name + credentials


def _is_press_worthy_link(host: str) -> bool:
    """A public_links host counts as a press mention only if it's neither a social
    network nor a people-directory aggregator (those are profiles, not news). Tests
    the registrable domain too, so a broker sub-domain (app.getwarmer.com) can't slip
    past an exact-host check."""
    if not host:
        return False
    blocked = _SOCIAL_HOSTS | _DIRECTORY_HOSTS
    return host not in blocked and registrable_host(host) not in blocked


def news_items(claims: list[ClaimRow], name: str = "") -> list[ClaimRow]:
    """The claims to curate: every news_mention, plus public_links whose host is a
    genuine content source (not a social profile or directory listing). Headlines
    that are firm boilerplate or just the person's name (a bio/profile page) are
    dropped — these are never articles, regardless of source."""
    items: list[ClaimRow] = []
    for c in claims:
        _, headline = _split_value(c.value)
        if _is_boilerplate_title(headline):
            continue  # firm/profile boilerplate is never news
        if name and _is_name_only_title(headline, name):
            continue  # a name-only heading is a bio page, not an article
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
feed scarce and high-signal: show ONLY items where THIS person is genuinely the \
story — what they personally said, did, were recognized for, or a career move they \
made. Most candidates should be cut. It is completely fine to keep nothing.

For each candidate (headline + snippet), decide four things:

1. subject_depth — how this article relates to THIS person. Choose EXACTLY ONE:
   - "feature" — the article is substantially ABOUT them: a profile, a Q&A or \
interview with them, a podcast episode featuring them, a piece centered on their \
move/deal/award. They are the subject from start to finish.
   - "substantive" — they are individually named with a REAL, describable point: a \
quote that states their actual view, a ranking/list that singles them out \
(highest-paid, top performers, 40-under-40), a promotion/hire/board seat, a deal \
they personally led or are quoted on. There is a specific insight you could write a \
one-line description from.
   - "passing" — they are named, but the mention carries no insight about them: a \
name in a long list, an attendee/alumni note, a one-word quote, a tangential \
reference. Nothing you'd tell a reader.
   - "not_about" — they are NOT the subject: news about their COMPANY \
(product launches, the firm's deals, "what the company is doing"), a profile/ \
directory/team page, a bio, a regulatory filing, or a different person who shares \
the name (a namesake). A page on the person's OWN firm site whose headline is just \
their name/title and whose text is marketing copy about them is a BIO, not press — \
set not_about.
   Only "feature" and "substantive" are shown. When in doubt between substantive \
and passing, choose passing. When in doubt whether it's even them, choose not_about.
   IMPORTANT: a person can be the subject even when the headline leads with their \
employer or an agency (e.g. "Highest-paid employees at X" that names them, or \
"X firm's CIO says ..."). Judge by whether THIS person is singled out with a real \
point, not by what the headline starts with.
2. category — choose EXACTLY ONE, copied verbatim:
   - "Funding & Deals" (a deal THEY led / are quoted on)
   - "Leadership Moves" (THEIR hire, promotion, new role, board seat, departure)
   - "Market Views" (THEIR commentary, outlook, interview, podcast)
   - "Recognition" (an award/ranking/honor naming THEM — including being \
individually named in a notable list: highest-paid, top performers, power lists, \
40-under-40, etc.)
   - "Company News" (about the firm with the person NOT individually featured — \
pair with subject_depth "not_about")
3. summary — ONE plain sentence (max ~25 words), leading with the PERSON, capturing \
the SPECIFIC point that makes this worth showing (their actual view, the exact \
honor, the role they moved into). No hype, no preamble, no vague "was mentioned in".
4. importance — float 0.0-1.0: how notable this is (a fund close, a major \
promotion, a named ranking, a real feature is high; a routine quote is mid; \
anything passing/not_about is low).

Return ONLY a JSON array, one object per candidate, SAME order:
[{"index": <int>, "subject_depth": "<feature|substantive|passing|not_about>", \
"category": "<one of the five>", "summary": "<one line>", "importance": <float>}]"""


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


# Verification stage: read the ACTUAL article around the person's name and confirm
# they are the subject before an item earns a slot. This is what catches an award
# page that names the person inside someone ELSE's entry ("Ross Willmann named to
# Forty Under Forty" — actually Chris Halaska's profile) and recovers the exact
# achievement a headline hides ("Investor, Coatue" -> "Forbes 30 Under 30 Finance").
_VERIFY_SYSTEM = """You fact-check ONE candidate news item for a finance alumni \
directory using the ARTICLE TEXT around where the person is named. Be strict: an \
item earns a slot only if THIS person is genuinely its subject.

Decide:
1. subject_depth — one of:
   - "feature": the article is substantially about this person.
   - "substantive": this person is individually named with a REAL, specific \
point you can verify in the text — an award/ranking/honor THEY received, being \
individually listed on a notable ranking (highest-paid, top performers, power list, \
40-under-40) WITH a figure/rank about them, a promotion / new role / board seat, or \
a deal/interview/podcast where they are personally quoted or featured.
   - "passing": this person is named but with no real point about them (a one-word \
mention, an attendee list with nothing specific).
   - "not_about": this person is NOT the subject. Use this when the page is about \
ONE OTHER named individual and merely name-drops this person inside THAT person's \
profile, award entry, quote, or team blurb (one specific other person is the \
honoree/subject), OR it is a firm/profile/directory page, OR a different person who \
shares the name.
   CRITICAL distinction: a multi-honoree LIST or RANKING that includes this person \
as one of its entries means THEY are a subject -> "substantive" (being listed IS \
the recognition). But a SINGLE other person's profile/award entry that quotes or \
mentions this person means this person is NOT the subject -> "not_about". The \
ARTICLE TEXT begins with the page HEAD (its title and who it is about); use it to \
identify the page's real subject. If the head shows the page/profile belongs to a \
DIFFERENT named individual (e.g. an award profile of "Chris Halaska") and your \
target is named only inside that person's answers, quotes, or "dream team" list, \
that is "not_about" — they did NOT receive the award. Ask: does the page present \
THIS person as one of its own honorees/subjects, or is it someone else's page that \
merely names them? When genuinely unsure, choose "not_about".
2. headline — a SHORT, accurate title taken from the article (e.g. the exact award \
+ category + year, or the real event). If the given headline is already accurate, \
reuse it. Never use a bare "Name - Title, Company" profile heading as the headline \
if the article shows a specific honor.
3. category — EXACTLY one: "Funding & Deals", "Leadership Moves", "Market Views", \
"Recognition", "Company News".
4. summary — ONE sentence (max ~25 words), led by the person, stating the EXACT \
thing they did or received (specific award name + category + year, the precise \
role/deal). No hype, no generic "is an investor at X".
5. importance — float 0.0-1.0. Calibrate: a named ranking/major award, a notable \
promotion or leadership move, or a real feature about them is high (0.7-0.9); an \
interview/podcast/commentary where they share their views, or being individually \
listed on a notable ranking, is solid (0.55-0.7); anything you marked passing or \
not_about is low (<0.4).

Return ONLY JSON: {"subject_depth":"..","headline":"..","category":"..","summary":"..","importance":0.0}"""


def _build_verify_user(
    name: str, employer: str, headline: str, snippet: str, article: str
) -> str:
    lines = [
        f"Person: {name}",
        f"Known employer: {employer or '(unknown)'}",
        f"Candidate headline: {headline}",
    ]
    if snippet:
        lines.append(f"Snippet: {snippet[:300]}")
    if article:
        lines.append("")
        lines.append("ARTICLE TEXT (around where the person is named):")
        lines.append(article[:2400])
    else:
        lines.append("")
        lines.append("ARTICLE TEXT: (could not be retrieved — judge conservatively)")
    return "\n".join(lines)


def _verify_item(
    client: Anthropic,
    name: str,
    employer: str,
    rank: float,
    item: CuratedNews,
    snippet: str,
    article: str,
    *,
    model: str,
    max_tokens: int,
) -> tuple[tuple[float, CuratedNews] | None, int, int]:
    """Confirm one would-be-shown item against the article. Returns (kept, in, out)
    where kept is a (rank, CuratedNews) to keep or None to drop.

    A MISSING article -> DROP: the triage verdict judged subject-depth from only the
    headline+snippet, which is exactly what mis-attributed someone else's award (the
    Forty-Under-Forty case). With no article to confirm the person is the subject, the
    item hasn't met the bar, so we don't show it. The feed is re-curated each run, so a
    transient scrape failure self-heals; precision is preferred over showing unverified.

    A model/parse FAILURE after a successful fetch -> conservatively KEEP: we did have
    an article and only the LLM call was flaky (re-running verifies); this is a
    transient hiccup, not the never-read-the-article hole above."""
    if not article:
        return None, 0, 0
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=0,
            system=[{"type": "text", "text": _VERIFY_SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _build_verify_user(
                name, employer, item.headline, snippet, article
            )}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
        ti, to = resp.usage.input_tokens, resp.usage.output_tokens
    except Exception:
        return (rank, item), 0, 0

    verdict = _parse_one(text)
    if verdict is None:
        return (rank, item), ti, to  # parse failed -> keep cheap verdict

    depth = str(verdict.get("subject_depth") or "").strip().lower()
    if depth not in _SHOWN_DEPTHS:
        return None, ti, to  # the article says it's not really about them -> drop
    category = verdict.get("category")
    if category not in _FEED_CATEGORIES:
        return None, ti, to
    importance = _clamp_importance(verdict.get("importance"), item.importance)
    if importance < NEWS_MIN_IMPORTANCE:
        return None, ti, to
    headline = str(verdict.get("headline") or "").strip() or item.headline
    summary = str(verdict.get("summary") or "").strip() or item.summary
    corrected = CuratedNews(
        headline=headline,
        summary=summary,
        category=category,
        date=item.date,
        source_url=item.source_url,
        source_host=item.source_host,
        importance=importance,
    )
    return (_DEPTH_RANK.get(depth, 0.0), corrected), ti, to


def _parse_one(text: str) -> dict | None:
    """Parse a single JSON object from a model reply (fenced or bare)."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        obj = json.loads(cleaned)
    except json.JSONDecodeError:
        s, e = cleaned.find("{"), cleaned.rfind("}")
        if not (0 <= s < e):
            return None
        try:
            obj = json.loads(cleaned[s : e + 1])
        except json.JSONDecodeError:
            return None
    return obj if isinstance(obj, dict) else None


def curate_news(
    client: Anthropic | None,
    name: str,
    employer: str,
    mentions: list[ClaimRow],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 1024,
    fetch_article: Callable[[str], str] | None = None,
) -> tuple[list[CuratedNews], int, int]:
    """Curate a person's press mentions. Returns (curated, haiku_in, haiku_out).
    Reads news_mention claims AND press-worthy public_links (see news_items), so
    Perplexity-discovered articles/podcasts surface, not just the Firecrawl press
    pass. Every article yields a row (model verdict, or a deterministic fallback).
    Returns ([], 0, 0) when there are no mentions."""
    items = news_items(mentions, name)
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
                # Deterministic editorial judgment: temperature 0 so a clear keeper
                # (e.g. a named ranking, a podcast feature) doesn't flicker in and
                # out of the feed between re-curation runs.
                temperature=0,
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

    curated: list[tuple[float, CuratedNews, str]] = []  # (rank, item, snippet)
    for i, ((date, headline), claim) in enumerate(zip(articles, items)):
        verdict = parsed.get(i)
        # No model judgment -> drop. The feed is deliberately scarce; an item earns
        # its slot, it isn't shown by default.
        if not verdict:
            continue
        # Public-records / salary-database hosts are never editorial news, whatever
        # the model decided. Drop them deterministically (e.g. a govt-salary list).
        if _host(claim.source_url) in _PUBLIC_RECORDS_HOSTS:
            continue
        depth = str(verdict.get("subject_depth") or "").strip().lower()
        # Only items where the person is the story (feature/substantive) make the
        # feed; "passing" mentions and "not_about" (firm/namesake) are dropped.
        if depth not in _SHOWN_DEPTHS:
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
        curated.append((_DEPTH_RANK.get(depth, 0.0), CuratedNews(
            headline=headline,
            summary=summary,
            category=category,
            date=date,
            source_url=claim.source_url,
            source_host=_host(claim.source_url),
            importance=importance,
        ), claim.quote or ""))
    # Features lead substantive at equal importance; otherwise rank by importance.
    curated.sort(key=lambda d: (d[0], d[1].importance), reverse=True)

    # Verification stage: this snippet-level pass is only a TRIAGE. Before an item
    # is shown, read the real article around the person's name and confirm they are
    # its subject (not name-dropped in someone else's entry) and fix the headline/
    # summary to the exact achievement. Only the would-be-shown items are fetched
    # (cost is bounded to the feed size), with a small buffer for verify drops.
    if fetch_article is not None and client is not None:
        verified: list[tuple[float, CuratedNews]] = []
        for rank, item, snippet in curated[: MAX_NEWS_PER_PERSON + 2]:
            article = name_window(fetch_article(item.source_url), name)
            kept, vin, vout = _verify_item(
                client, name, employer, rank, item, snippet, article,
                model=model, max_tokens=512,
            )
            tok_in += vin
            tok_out += vout
            if kept is not None:
                verified.append(kept)
        verified.sort(key=lambda d: (d[0], d[1].importance), reverse=True)
        return [c for _, c in verified[:MAX_NEWS_PER_PERSON]], tok_in, tok_out

    # No fetcher (e.g. backfill without scraping): fall back to the triage verdict.
    return [c for _, c, _ in curated[:MAX_NEWS_PER_PERSON]], tok_in, tok_out
