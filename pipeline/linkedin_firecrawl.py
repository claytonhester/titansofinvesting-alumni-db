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

import re
from dataclasses import dataclass

from firecrawl import Firecrawl
from firecrawl.v2.utils.error_handler import PaymentRequiredError

from enrichment_store import ClaimRow

# Regex patterns for detecting "present" career entries in value/quote fields.
# value format: "Title at Company (YYYY-present)"
_PRESENT_VALUE_RE = re.compile(
    r"\((\d{4})\s*[-–]\s*(?:now|present)\)", re.IGNORECASE
)
# quote format: "YYYY - present Title @ Company" (resume.ts QUOTE_RE shape)
_PRESENT_QUOTE_RE = re.compile(
    r"^(\d{4})\s*[-–]\s*(?:now|present)\b", re.IGNORECASE
)

EXTRACTION_METHOD = "firecrawl-linkedin"
# LinkedIn is authoritative for a person's own career, but it's a name-based agent
# lookup (namesake risk), so it sits just under a verified PDL likelihood match.
LINKEDIN_CONFIDENCE = 0.8

# The agent is BILLED and variable. Firecrawl does NOT reliably honor this per-call
# cap (a call capped at 60 was observed spending 324), so it's best-effort only —
# the real protection is the pre-flight LinkedInBudget below.
DEFAULT_MAX_CREDITS = 40
LINKEDIN_MIN_CAREER = 3
# Per-person allowance for the run-level agent budget. The agent only pays off on a
# minority of thin profiles, so a batch budget well under (cost-per-firing × N)
# caps total spend; the minimum still lets a single-person run fire once.
AGENT_CREDITS_PER_PERSON = 15


def _current_role_start_year_from_claims(claims) -> int | None:
    """Scan career_history claims for the one marked present/now and return its
    start year. Used by the year-gap heuristic when PDL doesn't supply the value."""
    for c in claims:
        if c.claim_type != "career_history":
            continue
        m = _PRESENT_VALUE_RE.search(c.value or "")
        if m:
            return int(m.group(1))
        m = _PRESENT_QUOTE_RE.match(c.quote or "")
        if m:
            return int(m.group(1))
    return None


def profile_needs_linkedin(
    claims,
    *,
    min_career: int = LINKEDIN_MIN_CAREER,
    grad_year: int | None = None,
    current_role_start_year: int | None = None,
) -> bool:
    """True when Firecrawl-scrape + PDL have NOT produced a complete-enough profile.

    Primary checks (section-level): missing current employer, no education, or
    fewer than min_career career_history entries.

    Year-gap heuristic: even when the primary checks pass, fire the LinkedIn agent
    if the span from grad_year to current_role_start_year is long relative to the
    number of career entries we have. A large gap with few entries implies lost
    employers (the Komson/TRS pattern: PDL returned the current role but silently
    dropped 8+ years at a prior employer).

    Rule of thumb: expect at least one distinct employer per 4 career years.
    So a 12-year gap should have >= 3 entries; a 5-year gap >= 2, etc.
    The floor is always min_career so we never lower the absolute bar.
    """
    career = sum(1 for c in claims if c.claim_type == "career_history")
    has_employer = any(c.claim_type == "current_employer" for c in claims)
    has_education = any(c.claim_type == "education" for c in claims)

    if (not has_employer) or (not has_education) or (career < min_career):
        return True

    if grad_year is not None and current_role_start_year is not None:
        gap = current_role_start_year - grad_year
        if gap > 4:
            expected_min = max(min_career, gap // 4)
            if career < expected_min:
                return True

    return False


@dataclass(frozen=True)
class LinkedInDecision:
    """Whether to fire the billed agent for one person, and why (for the log)."""
    fire: bool
    reason: str


def agent_batch_budget(
    n_people: int,
    *,
    per_person: int = AGENT_CREDITS_PER_PERSON,
    minimum: int | None = None,
) -> int:
    """Total LinkedIn-agent credits a batch may spend. Scales with size but never
    below one full firing, so a single-person run isn't starved."""
    floor = DEFAULT_MAX_CREDITS + 20 if minimum is None else minimum
    return max(floor, per_person * max(0, n_people))


class LinkedInBudget:
    """Run-level hard ceiling on LinkedIn-agent spend. Because Firecrawl ignores
    the per-call cap, the only dependable control is pre-flight: stop firing once
    the batch budget is spent. One in-flight call can still overshoot by up to its
    own cost, so the effective worst case is (budget + one capped call).

    Mutable on purpose — it threads through the per-person loop accumulating spend.
    The skip gate also enforces a minimum verified-web-presence bar: firing the
    name-based agent on a person with ZERO identity-verified sources almost always
    burns credits for nothing (no public footprint to find)."""

    def __init__(self, total_credits: int, *, min_verified_sources: int = 1) -> None:
        self.remaining = max(0, total_credits)
        self.min_verified_sources = min_verified_sources

    def decide(
        self,
        claims,
        trusted_count: int,
        *,
        grad_year: int | None = None,
        current_role_start_year: int | None = None,
    ) -> LinkedInDecision:
        if not profile_needs_linkedin(
            claims,
            grad_year=grad_year,
            current_role_start_year=current_role_start_year,
        ):
            return LinkedInDecision(False, "profile already complete")
        if trusted_count < self.min_verified_sources:
            return LinkedInDecision(False, "no verified web presence")
        if self.remaining <= 0:
            return LinkedInDecision(False, "batch LinkedIn budget spent")
        return LinkedInDecision(True, "thin profile with web presence")

    def charge(self, credits: int) -> None:
        self.remaining = max(0, self.remaining - (credits or 0))

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
