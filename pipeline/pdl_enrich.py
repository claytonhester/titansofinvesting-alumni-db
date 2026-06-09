"""People Data Labs Person Enrichment adapter for Phase 2.

A plain REST source that DEEPENS the existing LinkedIn-derived résumé. PDL is
queried with name + company + city (all already on the person row) and returns a
structured, LinkedIn-class profile plus a match ``likelihood`` (0-10). Because PDL
self-disambiguates on those anchors, we treat it like the deterministic prefilter:
its facts are trusted only when the match clears a likelihood gate.

Two design choices keep this honest and cheap:

1. Identity gate, server-side. We pass ``min_likelihood`` so PDL returns 404 (and
   charges nothing) below the threshold — no namesake ever enters the résumé, and
   we don't pay for matches we'd discard. A returned 200 is double-checked locally.
2. Canonical claim_types only. PDL claims map onto the SAME claim_types the Haiku
   extractor already emits (current_title, current_employer, location,
   career_history, education, public_links), in the exact value/quote shapes
   web/lib/resume.ts parses. So PDL strengthens the current résumé UI with zero
   front-end work and no schema migration — just more rows before replace_claims.

Never raises: a PDL outage or bad response yields an empty result so the
Firecrawl/Sonnet path still completes. Public data only; no auth, no logins.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from enrichment_store import ClaimRow

PDL_ENRICH_URL = "https://api.peopledatalabs.com/v5/person/enrich"
EXTRACTION_METHOD = "pdl"

# Skills are noisy and long-tailed; keep the most relevant handful.
MAX_SKILLS = 12

# PDL likelihood is an integer 0-10. 6 is PDL's own recommended floor for a
# confident single-person match; below it the risk of a wrong person rises sharply.
PDL_ACCEPT = 6


@dataclass(frozen=True)
class PdlAttributes:
    """Single-valued profile attributes PDL returns alongside the résumé. These
    ride the match we already pay for; we simply stop discarding them. Stored on
    person_insights (not as claims) because they're structured attributes that
    drive aggregation, not discrete quote-backed résumé facts. Every field is
    optional — a PDL response that omits one just leaves it empty/None."""

    current_industry: str = ""
    current_company_size: str = ""
    job_function: str = ""          # PDL job_title_role (e.g. "finance")
    pdl_seniority: str = ""         # PDL job_title_levels joined (e.g. "director")
    current_role_start_year: int | None = None
    years_experience: int | None = None
    linkedin_connections: int | None = None


_EMPTY_ATTRS = PdlAttributes()


@dataclass(frozen=True)
class PdlResult:
    """One person's PDL outcome plus the real (billed) cost.

    ``matched`` is True only when PDL returned a 200 at/above the likelihood gate —
    which is also the only case PDL charges for, so ``cost_usd`` tracks ``matched``.
    ``claim_rows`` is empty on a miss, an outage, or a below-gate likelihood.
    ``attributes`` carries the structured extras (industry, tenure, etc.)."""

    claim_rows: tuple[ClaimRow, ...]
    likelihood: int
    matched: bool
    cost_usd: float
    attributes: PdlAttributes = _EMPTY_ATTRS


_EMPTY = PdlResult(claim_rows=(), likelihood=0, matched=False, cost_usd=0.0)


def enrich_pdl(
    client: httpx.Client,
    api_key: str,
    full_name: str,
    company: str,
    city: str,
    *,
    min_likelihood: int = PDL_ACCEPT,
    cost_usd_per_match: float,
    attempts: int = 3,
    backoff_base: float = 1.5,
) -> PdlResult:
    """Query PDL for one person, gate on likelihood, and map a confident match to
    canonical ClaimRows. Returns an empty result (no claims, no cost) on a miss,
    a below-gate likelihood, or any network/parse failure — never raises."""
    name = full_name.strip()
    if not name:
        return _EMPTY

    params: dict[str, str | int] = {"name": name, "min_likelihood": min_likelihood}
    if company and company != "(unknown)":
        params["company"] = company
    if city and city != "(unknown)":
        params["location"] = city

    payload = _get_with_retry(client, api_key, params, attempts, backoff_base)
    if payload is None:
        return _EMPTY

    likelihood = _as_int(payload.get("likelihood"))
    data = payload.get("data")
    if not isinstance(data, dict) or likelihood < min_likelihood:
        # PDL billed for the 200 even if our local re-check rejects it; record the
        # cost but weave in nothing.
        return PdlResult(claim_rows=(), likelihood=likelihood, matched=True, cost_usd=cost_usd_per_match)

    confidence = max(0.0, min(1.0, likelihood / 10.0))
    source_url = _profile_url(data)
    rows = _map_claims(data, confidence, source_url)
    return PdlResult(
        claim_rows=tuple(rows),
        likelihood=likelihood,
        matched=True,
        cost_usd=cost_usd_per_match,
        attributes=_extract_attributes(data),
    )


def _get_with_retry(
    client: httpx.Client,
    api_key: str,
    params: dict[str, str | int],
    attempts: int,
    backoff_base: float,
) -> dict | None:
    """One PDL enrich GET. 200 -> parsed JSON; 404 (no match at/above the gate) ->
    None and NO charge; transient failures back off then yield None. Never raises:
    a PDL hiccup degrades this person's enrichment, it doesn't abort the run."""
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    for attempt in range(attempts):
        try:
            resp = client.get(PDL_ENRICH_URL, params=params, headers=headers)
        except Exception:
            if attempt == attempts - 1:
                return None
            time.sleep(backoff_base ** attempt)
            continue

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                return None
            return body if isinstance(body, dict) else None
        if resp.status_code == 404:
            return None  # no confident match — free, by design
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == attempts - 1:
                return None
            time.sleep(backoff_base ** attempt)
            continue
        # 4xx other than 404/429 (bad key, bad request): retrying can't help.
        return None
    return None


def _map_claims(data: dict, confidence: float, source_url: str) -> list[ClaimRow]:
    """Map a confident PDL person record onto the canonical claim_types in the
    exact value/quote shapes web/lib/resume.ts already parses."""
    rows: list[ClaimRow] = []

    title = _clean(data.get("job_title"))
    employer = _clean(data.get("job_company_name"))
    location = _clean(data.get("location_name"))

    if title:
        rows.append(_claim("current_title", title, source_url, "", confidence))
    if employer:
        rows.append(_claim("current_employer", employer, source_url, "", confidence))
    if location:
        rows.append(_claim("location", location, source_url, "", confidence))

    for entry in data.get("experience") or []:
        row = _experience_claim(entry, source_url, confidence)
        if row is not None:
            rows.append(row)

    for entry in data.get("education") or []:
        row = _education_claim(entry, source_url, confidence)
        if row is not None:
            rows.append(row)

    for link in _public_links(data):
        rows.append(_claim("public_links", link[0], link[1], "", confidence))

    for skill in _skills(data):
        rows.append(_claim("skill", skill, source_url, "", confidence))

    for cert in _certifications(data):
        rows.append(_claim("certification", cert, source_url, "", confidence))

    return rows


def _skills(data: dict) -> list[str]:
    """Up to MAX_SKILLS cleaned skill strings, de-duplicated, order preserved."""
    out: list[str] = []
    seen: set[str] = set()
    for s in data.get("skills") or []:
        val = _clean(s)
        key = val.lower()
        if val and key not in seen:
            seen.add(key)
            out.append(val)
        if len(out) >= MAX_SKILLS:
            break
    return out


def _certifications(data: dict) -> list[str]:
    """Certification names. PDL items may be strings or {name: ...} objects; both
    are handled, and absence is fine (the field isn't on every plan)."""
    out: list[str] = []
    seen: set[str] = set()
    for c in data.get("certifications") or []:
        if isinstance(c, dict):
            val = _clean(c.get("name"))
        else:
            val = _clean(c)
        key = val.lower()
        if val and key not in seen:
            seen.add(key)
            out.append(val)
    return out


def _extract_attributes(data: dict) -> PdlAttributes:
    """Pull the single-valued profile extras PDL already returns. Defensive: every
    field falls back to empty/None when PDL omits it (varies by plan/record)."""
    levels = data.get("job_title_levels")
    seniority = ", ".join(_clean(x) for x in levels if _clean(x)) if isinstance(levels, list) else ""
    start_year = _year(data.get("job_start_date"))
    return PdlAttributes(
        current_industry=_clean(data.get("job_company_industry")) or _clean(data.get("industry")),
        current_company_size=_clean(data.get("job_company_size")),
        job_function=_clean(data.get("job_title_role")),
        pdl_seniority=seniority,
        current_role_start_year=int(start_year) if start_year else None,
        # _opt_int keeps a real 0 as 0; only missing/invalid becomes None, so a
        # legitimate "0 years" / "0 connections" isn't silently lost.
        years_experience=_opt_int(data.get("inferred_years_experience")),
        linkedin_connections=_opt_int(data.get("linkedin_connections")),
    )


def _experience_claim(entry: object, source_url: str, confidence: float) -> ClaimRow | None:
    """Build a career_history claim in resume.ts's parse shapes. When both years
    are known we emit the quote form ("YYYY - end Title @ Company"), which the UI
    parses first; otherwise a plain "Title at Company" still renders as a role."""
    if not isinstance(entry, dict):
        return None
    title = _clean(_nested(entry.get("title"), "name"))
    company = _clean(_nested(entry.get("company"), "name"))
    if not title and not company:
        return None

    label = title or company
    company_part = company or ""
    start = _year(entry.get("start_date"))
    is_primary = bool(entry.get("is_primary"))
    end = "present" if (is_primary or entry.get("end_date") is None and start) else _year(entry.get("end_date"))

    if start and company_part:
        end_tok = end or "present"
        value = f"{label} at {company_part} ({start}-{end_tok})"
        quote = f"{start} - {end_tok} {label} @ {company_part}"
        return _claim("career_history", value, source_url, quote, confidence)
    if company_part:
        return _claim("career_history", f"{label} at {company_part}", source_url, "", confidence)
    return _claim("career_history", label, source_url, "", confidence)


def _education_claim(entry: object, source_url: str, confidence: float) -> ClaimRow | None:
    """Build an education claim as "{degree} from {institution}" (resume.ts splits
    on ' from '); fall back to the institution alone when no degree is present.

    The graduation (end) year, when PDL has it, goes in the quote — keeping the
    display value clean while giving grad_year derivation a VERIFIED year for
    matched people instead of the school-aware class-map guess."""
    if not isinstance(entry, dict):
        return None
    institution = _clean(_nested(entry.get("school"), "name"))
    if not institution:
        return None
    degrees = entry.get("degrees") or []
    degree = _clean(degrees[0]) if degrees and isinstance(degrees[0], str) else ""
    value = f"{degree} from {institution}" if degree else institution
    end_year = _year(entry.get("end_date"))
    quote = f"Graduated {end_year}" if end_year else ""
    return _claim("education", value, source_url, quote, confidence)


def _public_links(data: dict) -> list[tuple[str, str]]:
    """(label, url) pairs for public_links: LinkedIn first (also lets the UI's
    findLinkedIn surface it), then any other linked public profiles."""
    out: list[tuple[str, str]] = []
    linkedin = _clean(data.get("linkedin_url"))
    if linkedin:
        url = linkedin if linkedin.startswith("http") else f"https://{linkedin}"
        out.append(("LinkedIn", url))
    for profile in data.get("profiles") or []:
        if not isinstance(profile, dict):
            continue
        network = _clean(profile.get("network"))
        url = _clean(profile.get("url"))
        if not url or network.lower() == "linkedin":
            continue
        full = url if url.startswith("http") else f"https://{url}"
        label = network.capitalize() if network else full
        out.append((label, full))
    return out


def _profile_url(data: dict) -> str:
    linkedin = _clean(data.get("linkedin_url"))
    if linkedin:
        return linkedin if linkedin.startswith("http") else f"https://{linkedin}"
    return ""


def _claim(claim_type: str, value: str, source_url: str, quote: str, confidence: float) -> ClaimRow:
    return ClaimRow(
        claim_type=claim_type,
        value=value,
        source_url=source_url,
        quote=quote,
        confidence=confidence,
        extraction_method=EXTRACTION_METHOD,
    )


def _nested(node: object, key: str) -> object:
    return node.get(key) if isinstance(node, dict) else None


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _as_int(value: object) -> int:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _opt_int(value: object) -> int | None:
    """Like _as_int but preserves a real 0 and returns None (not 0) when the
    value is missing or unparseable — so a legitimate zero isn't lost."""
    if value is None:
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _year(value: object) -> str:
    """PDL dates are 'YYYY', 'YYYY-MM' or 'YYYY-MM-DD'. Return the 4-digit year."""
    text = _clean(value)
    return text[:4] if len(text) >= 4 and text[:4].isdigit() else ""
