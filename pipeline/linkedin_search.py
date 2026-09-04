"""Search-based LinkedIn URL finder — the move a human makes: search
"name + university + employer" and read the LinkedIn result off the page.

Motivation (pilot, 2026-06-11): PDL returns a `linkedin_url` but it is sometimes
a wrong guess (it gave `jean-luc-vandermeer`; the real profile is
`jlvandermeer`). A plain Perplexity search of name + school + employer surfaces
the correct URL and catches that error. Neither source is reliable alone — search
misses some people and returns namesakes for common names; PDL guesses wrong — so
the caller UNIONS both candidate sets and prefers the one with the strongest
independent corroboration (the search snippet naming the employer/school).

Pure-ish: one Perplexity /search call (~$0.005), no agent, no DB. Never raises.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from perplexity_enrich import fetch_perplexity

_LINKEDIN_IN_RE = re.compile(r"linkedin\.com/in/[^\s/?\"')]+", re.I)

# extraction_method for a search-resolved linkedin_url claim that has NOT been
# through the fail-closed verifier and is carried by a single search result.
# Persisted at low confidence for provenance only; nothing downstream may treat
# it as "this person's LinkedIn" (the batch-1 namesake-echo finding).
SEARCH_UNVERIFIED_METHOD = "search-unverified"
SEARCH_UNVERIFIED_CONFIDENCE = 0.4
# A search-resolved URL counts as corroborated when at least this many DISTINCT
# search results (different pages) carry the same profile slug.
MIN_INDEPENDENT_HITS = 2
# A token must be at least this long to count as an employer/school match, so
# "co"/"of"/"the" can't manufacture a false corroboration.
_MIN_TOKEN = 4


@dataclass(frozen=True)
class LinkedInCandidate:
    url: str        # normalized https://linkedin.com/in/<slug>
    score: float    # higher = more corroborated by the search snippet
    evidence: str   # which anchors matched (name/employer/school/slug)
    source: str     # "search" or "pdl"
    hits: int = 1   # distinct search results carrying this URL (independent corroboration)


def _normalize(raw: str) -> str:
    m = _LINKEDIN_IN_RE.search(raw or "")
    if not m:
        return ""
    return "https://" + m.group(0).rstrip("/").lower()


def _tokens(text: str) -> set[str]:
    return {t for t in re.split(r"[^a-z0-9]+", (text or "").lower()) if len(t) >= _MIN_TOKEN}


def _score(url: str, text: str, *, last: str, employer: str, school: str) -> tuple[float, str]:
    text_l = text.lower()
    score = 0.0
    ev: list[str] = []
    if last and last in text_l:
        score += 1.0
        ev.append("name")
    if last and last in url.lower():
        score += 0.5
        ev.append("slug")
    emp_tokens = _tokens(employer)
    if emp_tokens and emp_tokens & _tokens(text):
        score += 1.0
        ev.append("employer")
    school_tokens = _tokens(school)
    if school_tokens and school_tokens & _tokens(text):
        score += 0.5
        ev.append("school")
    return score, ",".join(ev)


def search_linkedin_candidates(
    http: httpx.Client,
    api_key: str | None,
    name: str,
    *,
    school: str = "",
    employer: str = "",
    max_results: int = 8,
) -> list[LinkedInCandidate]:
    """Perplexity search of name + school + employer → ranked LinkedIn candidates.
    Empty list when the key is missing or nothing matches. Never raises."""
    if not api_key or not name.strip():
        return []
    hint = " ".join(x for x in (school, employer) if x and x.strip()) or None
    results = fetch_perplexity(http, api_key, name, employer=hint, max_results=max_results)
    last = name.strip().split()[-1].lower() if name.strip().split() else ""
    best: dict[str, LinkedInCandidate] = {}
    # Which distinct result pages carried each profile URL. The LinkedIn page
    # itself plus, say, a firm bio linking to it = two independent sources; the
    # same page twice is one.
    pages: dict[str, set[str]] = {}
    for r in results:
        url = _normalize(r.url) or _normalize(r.snippet)
        if not url:
            continue
        score, ev = _score(url, f"{r.title} {r.snippet}", last=last,
                            employer=employer, school=school)
        pages.setdefault(url, set()).add((r.url or "").strip().lower() or url)
        cand = LinkedInCandidate(url, score, ev, "search")
        if url not in best or score > best[url].score:
            best[url] = cand
    ranked = [
        LinkedInCandidate(c.url, c.score, c.evidence, c.source, hits=len(pages[c.url]))
        for c in best.values()
    ]
    return sorted(ranked, key=lambda c: -c.score)


def choose_linkedin_url(
    pdl_url: str,
    candidates: list[LinkedInCandidate],
    *,
    min_corroboration: float = 2.0,
) -> tuple[str, str]:
    """Pick the best LinkedIn URL from PDL's guess + the search candidates.

    Logic: a search hit that names BOTH the person and their employer (score >=
    min_corroboration) is trusted even over PDL — that's what catches PDL's wrong
    guesses. If PDL's URL also appears in the search results, they agree and PDL
    stands. Otherwise fall back to PDL's URL (it's right more often than a weak,
    namesake-prone search hit). Returns (url, reason)."""
    pdl_norm = _normalize(pdl_url) if pdl_url else ""
    if not candidates:
        return pdl_norm, "pdl-only (no search hits)"
    top = candidates[0]
    search_urls = {c.url for c in candidates}
    if pdl_norm and pdl_norm in search_urls:
        return pdl_norm, "pdl confirmed by search"
    if top.score >= min_corroboration:
        # Ambiguous: several equally-corroborated profiles (common name with no
        # distinguishing employer — the Nora Whitfield case). Don't guess; keep
        # PDL and let the downstream verifier / human resolve it.
        tied = [c for c in candidates if c.score == top.score]
        if len(tied) > 1 and pdl_norm not in {c.url for c in tied}:
            return pdl_norm, f"ambiguous ({len(tied)} tied) — kept PDL" if pdl_norm \
                else f"ambiguous ({len(tied)} tied) — no pick"
        if pdl_norm and pdl_norm != top.url:
            return top.url, f"search overrides PDL ({top.evidence})"
        return top.url, f"search ({top.evidence})"
    return (pdl_norm or top.url,
            "pdl fallback (weak search)" if pdl_norm else "best search")
