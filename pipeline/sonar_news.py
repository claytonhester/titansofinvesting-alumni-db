"""Perplexity Sonar press-discovery source for Phase 2.

The bake-off (sonar_probe.py, see [[project_titans_stack_decisions_jun9]]) found
Sonar-pro is a poor *résumé* source (non-deterministic on findable people) but a
strong *focused-press* source: cited, person-specific recognition (Forty Under
Forty, promotions, podcasts, interviews) at ~$0.008/person — and crucially OFF the
Firecrawl credit budget. This is that keeper, wired for production.

The flow mirrors mention_discovery but uses Sonar instead of /search:

    Sonar-pro focused-press prompt (web-grounded, cited)
        -> per-item is_about_this_person gate (Sonar's own namesake reasoning)
        -> drop people-search / data-broker domains  (news_score)
        -> emit as ``news_mention`` claims

The emitted claims flow into the SAME strict Haiku curator (news_curate) as the
Firecrawl press pass and the /search mentions, where the subject_depth taxonomy
(feature / substantive / passing / not_about) is the final editorial gate. So a
press item survives only if BOTH Sonar (is it this person?) and the curator (is the
person the story?) agree — defense in depth against namesakes and filler.

Never raises: a missing key, an outage, or malformed JSON yields an empty result so
a bulk enrichment loop degrades instead of aborting.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

import httpx

from enrichment_store import ClaimRow
from news_score import has_meaningful_employer, is_aggregator_domain

SONAR_URL = "https://api.perplexity.ai/chat/completions"
DEFAULT_MODEL = "sonar-pro"
CLAIM_TYPE = "news_mention"
EXTRACTION_METHOD = "sonar-pro"
# A press hit Sonar vouches for, but still a public "mention", not a hard fact.
PRESS_CONFIDENCE = 0.6
_DATE_SEP = " — "  # matches news_curate / web/lib/news.ts

# USD per 1M tokens (input, output) + per-request fee, used only when the API does
# not report an authoritative usage.cost (it usually does). sonar-pro, medium tier.
_PRICE_IN = 3.0
_PRICE_OUT = 15.0
_PRICE_REQUEST = 0.010


@dataclass(frozen=True)
class SonarPressResult:
    """Outcome for one person. Counts are for logging/cost visibility."""

    claim_rows: tuple[ClaimRow, ...]
    found: int          # raw press items Sonar returned
    kept: int           # after the is_about + aggregator-domain gates
    cost_usd: float     # authoritative usage.cost when present, else token-priced
    requests: int       # Sonar calls issued (0 or 1 per person)


_EMPTY = SonarPressResult(claim_rows=(), found=0, kept=0, cost_usd=0.0, requests=0)

_SYSTEM = (
    "You surface NOTABLE, publicly-documented items about ONE specific person, "
    "reporting only what cited public sources support. You "
    "are not writing a profile — you surface specific, sourced items where THIS "
    "person is individually the subject. If you cannot tell this person apart from a "
    "namesake, set is_about_this_person=false. Never invent headlines, dates, or "
    "URLs; every item must come from a real source you can cite."
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "headline": {"type": "string"},
                    "url": {"type": "string"},
                    "date": {"type": "string"},
                    "why": {"type": "string"},
                    "is_about_this_person": {"type": "boolean"},
                },
                "required": [
                    "headline", "url", "date", "why", "is_about_this_person",
                ],
            },
        },
    },
    "required": ["items"],
}


# A live A/B (Silvio Canto) showed targeted per-facet prompts surface items a single
# combined prompt misses (a conference keynote + a People-on-the-Move + a panel that
# the catch-all never returned). So we run a SMALL set of focused asks, each ~$0.008,
# and pool the results — recall up, cost bounded, the curator still the strict gate.
_FACETS: tuple[str, ...] = (
    "a significant award, honor, ranking, or industry recognition that names them, "
    "OR a promotion, new role, or a firm/fund they founded or launched",
    "speaking or voice: a conference panel or keynote, a podcast or media "
    "appearance, an interview, OR thought leadership they authored (op-ed, article, "
    "whitepaper, book)",
    "a deal, transaction, fundraise, or IPO they personally led or are quoted on, "
    "OR a board seat, advisory role, or notable nonprofit / community leadership",
)

# Cap on targeted per-past-company asks (multi-company / career-spanning search).
_MAX_TARGETED_COMPANIES = 3


def _targeted_company_asks(
    past_companies: tuple[str, ...], employer: str,
) -> tuple[str, ...]:
    """One focused ask per real PAST firm (career-spanning search), capped. Skips
    blanks and any company equal to the current employer (already covered by the
    thematic facets)."""
    current = (employer or "").strip().lower()
    asks: list[str] = []
    seen: set[str] = set()
    for raw in past_companies:
        name = (raw or "").strip()
        key = name.lower()
        if not name or key == current or key in seen:
            continue
        seen.add(key)
        asks.append(
            f"any item connecting them to {name} — a role, deal, recognition, or "
            f"commentary tied to their time there"
        )
        if len(asks) >= _MAX_TARGETED_COMPANIES:
            break
    return tuple(asks)


def _user(
    name: str, employer: str, city: str, ask: str,
    role: str = "", industry: str = "",
) -> str:
    emp = employer.strip() if has_meaningful_employer(employer) else ""
    return (
        f"Treat '{name}' as ONE person's full name (first + last) — not a place or "
        f"company. Person: {name}. Role: {role.strip() or '(unknown)'}. "
        f"Field/industry: {industry.strip() or '(unknown)'}. "
        f"Known employer: {emp or '(unknown)'}. City: {city or '(unknown)'}.\n\n"
        f"Find items where this person is INDIVIDUALLY the subject — specifically: {ask}.\n\n"
        "Do NOT include: news about their company where they are not named, "
        "team/leadership/bio/profile pages, directory listings, regulatory filings, "
        "or items about a different person with the same name.\n\n"
        "For each item set is_about_this_person=true only if you are confident it "
        "is THIS person (not a namesake). Put the SPECIFIC point about them in "
        "'why' (the exact honor, the deal, the role, their stated view). Use an ISO "
        "date (YYYY-MM-DD) when known, else an empty string. Return ONLY the JSON "
        "object with the 'items' array; an empty array is correct when there is "
        "nothing for this facet."
    )


def _loads_lenient(blob: str) -> dict | None:
    blob = blob.strip()
    if blob.startswith("```"):
        blob = blob.split("\n", 1)[-1]
        if blob.endswith("```"):
            blob = blob[:-3]
    try:
        return json.loads(blob)
    except Exception:
        s, e = blob.find("{"), blob.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(blob[s : e + 1])
            except Exception:
                return None
    return None


def _cost(usage: dict) -> float:
    """Authoritative cost from usage.cost.total_cost when Perplexity reports it;
    otherwise price the tokens we captured plus the per-request fee."""
    reported = usage.get("cost")
    if isinstance(reported, dict):
        total = reported.get("total_cost")
        if isinstance(total, (int, float)):
            return float(total)
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    return (pt / 1e6) * _PRICE_IN + (ct / 1e6) * _PRICE_OUT + _PRICE_REQUEST


def _press_claim(item: object) -> ClaimRow | None:
    """One Sonar press item -> a news_mention claim, or None if it fails the
    is_about gate, is an aggregator/data-broker page, or lacks a headline+url."""
    if not isinstance(item, dict):
        return None
    if not bool(item.get("is_about_this_person")):
        return None
    headline = str(item.get("headline") or "").strip()
    url = str(item.get("url") or "").strip()
    if not headline or not url:
        return None
    if is_aggregator_domain(url):
        return None
    date = str(item.get("date") or "").strip()
    value = f"{date}{_DATE_SEP}{headline}" if _is_iso_date(date) else headline
    return ClaimRow(
        claim_type=CLAIM_TYPE,
        value=value,
        source_url=url,
        quote=str(item.get("why") or "").strip(),
        confidence=PRESS_CONFIDENCE,
        extraction_method=EXTRACTION_METHOD,
    )


def _is_iso_date(s: str) -> bool:
    return len(s) == 10 and s[4] == "-" and s[7] == "-" and s.replace("-", "").isdigit()


def _one_facet(
    http: httpx.Client, key: str, name: str, employer: str, city: str, ask: str,
    model: str, timeout: float, role: str = "", industry: str = "",
) -> tuple[list[dict], float]:
    """One focused Sonar call → (raw items, cost). Never raises: ([], 0.0) on fail."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user(name, employer, city, ask, role, industry)},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"schema": _SCHEMA}},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        resp = http.post(SONAR_URL, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        body = resp.json()
    except Exception:
        return [], 0.0
    cost = _cost(body.get("usage") or {})
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    parsed = _loads_lenient(content) or {}
    items = parsed.get("items") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        items = parsed.get("press") if isinstance(parsed, dict) else None  # back-compat
    return (items if isinstance(items, list) else []), cost


def _canon_url(url: str) -> str:
    u = (url or "").strip().lower().rstrip("/")
    return u.split("?")[0]


def discover_press_sonar(
    http: httpx.Client,
    full_name: str,
    employer: str,
    city: str,
    *,
    perplexity_key: str | None,
    model: str = DEFAULT_MODEL,
    timeout: float = 90.0,
    facets: tuple[str, ...] | None = None,
    role: str = "",
    industry: str = "",
    past_companies: tuple[str, ...] = (),
) -> SonarPressResult:
    """Surface cited, person-specific notable items via Sonar — one focused call per
    facet (awards/moves, speaking/voice, deals/boards) PLUS up to three targeted
    asks against the person's real past firms (career-spanning search), pooled and
    de-duped by URL. Queries adapt to the person's actual role/industry (no hardcoded
    field). Each item still passes the is_about gate + aggregator drop here, then the
    article-verified curator downstream. Empty (no request/cost) when key or name is
    missing; degrades to empty — never raises — on any API/parse failure."""
    if not perplexity_key or not full_name.strip():
        return _EMPTY

    thematic = facets if facets is not None else _FACETS
    asks = tuple(thematic) + _targeted_company_asks(past_companies, employer)
    cost = 0.0
    requests = 0
    seen: set[str] = set()
    items: list[dict] = []
    for ask in asks:
        raw, c = _one_facet(
            http, perplexity_key, full_name, employer, city, ask, model, timeout,
            role=role, industry=industry,
        )
        cost += c
        requests += 1
        for it in raw:
            if not isinstance(it, dict):
                continue
            key = _canon_url(str(it.get("url") or ""))
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            items.append(it)

    rows = [c for c in (_press_claim(it) for it in items) if c is not None]
    return SonarPressResult(
        claim_rows=tuple(rows),
        found=len(items),
        kept=len(rows),
        cost_usd=cost,
        requests=requests,
    )
