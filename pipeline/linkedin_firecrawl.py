"""Firecrawl agent-mode LinkedIn collector.

Firecrawl's plain scrape/extract are auth-walled out of LinkedIn, but its
``agent()`` mode CAN read a public LinkedIn profile. This pulls a person's
LinkedIn-derived résumé — current role, full experience, education, profile URL —
as a CORE source ALONGSIDE PDL. The reconciler then merges the two (LinkedIn is
live/current; PDL is the aggregated record), and the deterministic digest cleans
the result.

Billed in Firecrawl credits per agent run (an agent browses, so it costs more
than a single scrape — ``credits_used`` is returned so the caller can meter it).
Key- and credit-gated and never-raises for non-payment failures: any error other
than "out of credits" yields an empty result so the enrichment loop continues on
PDL + Perplexity. A 0-credit state raises PaymentRequiredError so the caller can
log it the same way it handles the other Firecrawl passes.

Maps onto the SAME canonical claim_types (current_title/current_employer/location/
career_history/education/public_links) in the exact value+quote shapes
web/lib/resume.ts parses — so it strengthens the résumé with zero front-end work.

Validated against the firecrawl 4.28.2 SDK:
``client.agent(prompt=..., schema=...) -> AgentResponse{data, credits_used,
status, success, error}``. First exercised live once Firecrawl has credits.
"""
from __future__ import annotations

from dataclasses import dataclass

from firecrawl import Firecrawl
from firecrawl.v2.utils.error_handler import PaymentRequiredError

from enrichment_store import ClaimRow

EXTRACTION_METHOD = "firecrawl-linkedin"
# LinkedIn is authoritative for a person's own career, but it's a name-based agent
# lookup (namesake risk), so it sits just under a verified PDL likelihood match.
LINKEDIN_CONFIDENCE = 0.8

# The agent is BILLED and variable (observed 45–324 credits/call), so cap each run
# and only fire it when the profile is still thin. A rich profile would just get a
# duplicate of what PDL already gave.
DEFAULT_MAX_CREDITS = 60
LINKEDIN_MIN_CAREER = 3


def profile_needs_linkedin(claims, *, min_career: int = LINKEDIN_MIN_CAREER) -> bool:
    """True when Firecrawl-scrape + PDL have NOT yet produced a complete-enough
    profile — i.e. a whole section is missing or roles are sparse. Section-level,
    not year-gap: missing current employer, no education, or < min_career roles.
    A complete profile returns False so we skip the (billed) agent."""
    career = sum(1 for c in claims if c.claim_type == "career_history")
    has_employer = any(c.claim_type == "current_employer" for c in claims)
    has_education = any(c.claim_type == "education" for c in claims)
    return (not has_employer) or (not has_education) or (career < min_career)

# JSON schema constraining the agent's output to a structured profile.
_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "linkedin_url": {"type": "string"},
        "current_title": {"type": "string"},
        "current_employer": {"type": "string"},
        "location": {"type": "string"},
        "experience": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "company": {"type": "string"},
                    "start_year": {"type": "string"},
                    "end_year": {"type": "string"},
                },
            },
        },
        "education": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "degree": {"type": "string"},
                    "school": {"type": "string"},
                },
            },
        },
    },
    "required": ["found"],
}


@dataclass(frozen=True)
class LinkedInResult:
    claim_rows: tuple[ClaimRow, ...]
    found: bool
    credits_used: int


_EMPTY = LinkedInResult(claim_rows=(), found=False, credits_used=0)


def build_prompt(name: str, employer: str, city: str) -> str:
    who = name.strip()
    qualifiers = []
    if employer and employer.strip() and employer.strip() != "(unknown)":
        qualifiers.append(f"who works (or worked) at {employer.strip()}")
    if city and city.strip() and city.strip() != "(unknown)":
        qualifiers.append(f"based in {city.strip()}")
    tail = (" " + " and ".join(qualifiers)) if qualifiers else ""
    return (
        f"Find the public LinkedIn profile for {who}{tail}. They are an alumnus of "
        "a Texas university finance/investing program. Return their current title, "
        "current employer, location, full work experience (title, company, start "
        "and end years), education (degree and school), and the LinkedIn profile "
        "URL. Set found=true ONLY if you confidently identify THIS person (not a "
        "namesake); otherwise set found=false and leave the other fields empty."
    )


def _as_dict(data: object) -> dict:
    """AgentResponse.data may be a dict or a pydantic model — normalize to dict."""
    if isinstance(data, dict):
        return data
    dump = getattr(data, "model_dump", None)
    if callable(dump):
        try:
            return dump()
        except Exception:
            return {}
    return {}


def _clean(value: object) -> str:
    return str(value).strip() if value is not None else ""


def _claim(claim_type: str, value: str, source_url: str, quote: str) -> ClaimRow:
    return ClaimRow(
        claim_type=claim_type,
        value=value,
        source_url=source_url,
        quote=quote,
        confidence=LINKEDIN_CONFIDENCE,
        extraction_method=EXTRACTION_METHOD,
    )


def _experience_claim(entry: object, source_url: str) -> ClaimRow | None:
    if not isinstance(entry, dict):
        return None
    title = _clean(entry.get("title"))
    company = _clean(entry.get("company"))
    if not title and not company:
        return None
    label = title or company
    start = _clean(entry.get("start_year"))[:4]
    end = _clean(entry.get("end_year"))[:4] or "present"
    if start and company:
        value = f"{label} at {company} ({start}-{end})"
        quote = f"{start} - {end} {label} @ {company}"  # resume.ts parses this first
        return _claim("career_history", value, source_url, quote)
    if company:
        return _claim("career_history", f"{label} at {company}", source_url, "")
    return _claim("career_history", label, source_url, "")


def _education_claim(entry: object, source_url: str) -> ClaimRow | None:
    if not isinstance(entry, dict):
        return None
    school = _clean(entry.get("school"))
    if not school:
        return None
    degree = _clean(entry.get("degree"))
    value = f"{degree} from {school}" if degree else school
    return _claim("education", value, source_url, "")


def map_claims(data: dict) -> list[ClaimRow]:
    """Map a found LinkedIn profile dict onto canonical ClaimRows. Pure — unit
    tested directly. Returns [] when the agent did not confidently find the person."""
    if not data or not data.get("found"):
        return []
    url = _clean(data.get("linkedin_url"))
    source = url if url.startswith("http") else (f"https://{url}" if url else "")

    rows: list[ClaimRow] = []
    title = _clean(data.get("current_title"))
    employer = _clean(data.get("current_employer"))
    location = _clean(data.get("location"))
    if title:
        rows.append(_claim("current_title", title, source, ""))
    if employer:
        rows.append(_claim("current_employer", employer, source, ""))
    if location:
        rows.append(_claim("location", location, source, ""))
    for entry in data.get("experience") or []:
        row = _experience_claim(entry, source)
        if row is not None:
            rows.append(row)
    for entry in data.get("education") or []:
        row = _education_claim(entry, source)
        if row is not None:
            rows.append(row)
    if source:
        rows.append(_claim("public_links", "LinkedIn", source, ""))
    return rows


def fetch_linkedin(
    client: Firecrawl,
    name: str,
    *,
    employer: str = "",
    city: str = "",
    timeout: int = 120,
    max_credits: int | None = DEFAULT_MAX_CREDITS,
) -> LinkedInResult:
    """Run one Firecrawl agent LinkedIn lookup for a person. Returns mapped claims
    plus the credits spent. `max_credits` caps the (variable, sometimes spiking)
    agent spend per call. Propagates PaymentRequiredError (so the caller logs "no
    credits" like the other Firecrawl passes); swallows every other error to an
    empty result."""
    if not name.strip():
        return _EMPTY
    try:
        resp = client.agent(
            prompt=build_prompt(name, employer, city),
            schema=_SCHEMA,
            timeout=timeout,
            max_credits=max_credits,
        )
    except PaymentRequiredError:
        raise
    except Exception:
        return _EMPTY

    if getattr(resp, "error", None) or getattr(resp, "status", "") not in ("completed", "", None):
        return LinkedInResult((), False, int(getattr(resp, "credits_used", 0) or 0))
    data = _as_dict(getattr(resp, "data", None))
    rows = map_claims(data)
    return LinkedInResult(
        claim_rows=tuple(rows),
        found=bool(data.get("found")),
        credits_used=int(getattr(resp, "credits_used", 0) or 0),
    )
