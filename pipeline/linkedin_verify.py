"""Roster-anchor identity gate for LinkedIn agent output — the keystone of
LinkedIn-first enrichment.

The Firecrawl agent finds A person by name search; nothing upstream has proven
it is OUR person. PDL claims clear a likelihood gate before extraction, so
pdl_verify can afford a keep-biased per-claim filter — a name-searched LinkedIn
cannot. This gate therefore inverts both choices:

  * ONE verdict for the WHOLE profile. The agent returned a single person; we
    judge that pick, not its individual lines. A wrong person's profile is
    wrong in its entirety.
  * FAIL-CLOSED. API error, malformed JSON, missing anchors -> rejected.
    A profile this gate cannot positively verify never becomes claims.
    (The old min-verified-sources gate on the agent existed because its output
    went into claims UNVERIFIED; this gate replaces that protection, which is
    what makes agent searches on thin/ghost profiles safe at all.)

Anchors come from the roster — facts a namesake almost never matches:
  * the school appears in education, in the graduation-year era (±4 years);
  * the roster first-employer appears in the early career;
  * the roster city appears somewhere in the history (soft corroboration).

Verdicts: "verified" (era + employer corroborate), "rejected" (an anchor
contradicts), "review" (partial/unclear — held for a human, never auto-used).
Every verdict is persisted to identity_candidates by the caller so the trail
is auditable like every other identity decision.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from enrichment_store import ClaimRow
from structuring import HAIKU_MODEL

DECISION_VERIFIED = "verified"
DECISION_REJECTED = "rejected"
DECISION_REVIEW = "review"
_VALID_DECISIONS = frozenset({DECISION_VERIFIED, DECISION_REJECTED, DECISION_REVIEW})

# How far a stated education year may drift from the roster class year and still
# corroborate: covers 5-year programs, masters years, and roster off-by-ones.
ERA_TOLERANCE_YEARS = 4

_SYSTEM = """A web agent searched LinkedIn for a specific person by name and \
returned ONE profile. Your job is to decide whether that profile is the SAME \
person as the target, using anchors from a trusted alumni roster. Namesakes are \
common; the roster anchors are facts a namesake almost never matches.

The target is an alumnus of a Texas university finance/investing program \
(Titans of Investing). You get their roster anchors and the profile's claimed \
education and career entries.

Decide ONE verdict for the whole profile:
- "verified" — the profile POSITIVELY matches: the school appears in education \
around the expected era, AND the roster employer (their job at graduation) \
appears in the early career. City agreement strengthens this but is not required.
- "rejected" — an anchor CONTRADICTS: a different school in that era, an \
education era that cannot be this person, an early career with no trace of the \
roster employer plus a profession that does not fit, or clear geography mismatch \
across the whole history.
- "review" — partial or unclear: one anchor matches but the other is absent \
(e.g. education matches but the early career is not shown). Do NOT guess; \
ambiguity goes to a human.

Be strict: "verified" requires positive corroboration, not mere absence of \
contradiction. A profile with no education and no early career shown can never \
be "verified".

Return ONLY a JSON object, no prose:
{"decision": "verified|rejected|review", "reason": "<short>", "confidence": <0.0-1.0>}"""


@dataclass(frozen=True)
class LinkedInVerdict:
    decision: str  # verified | rejected | review
    reason: str
    confidence: float

    @property
    def verified(self) -> bool:
        return self.decision == DECISION_VERIFIED


_REJECT_ON_ERROR = LinkedInVerdict(
    DECISION_REJECTED, "verifier failed — fail-closed", 0.0
)


def _build_user(
    name: str,
    profile_url: str,
    school: str,
    grad_year: int | None,
    roster_employer: str,
    city: str,
    claims: list[ClaimRow],
) -> str:
    era = (
        f"around {grad_year} (±{ERA_TOLERANCE_YEARS} years)"
        if grad_year
        else "(unknown)"
    )
    lines = [
        "Target person (roster anchors):",
        f"  Name: {name}",
        f"  School: {school or '(unknown)'}",
        f"  Graduation era: {era}",
        f"  Employer at graduation: {roster_employer or '(unknown)'}",
        f"  City on roster: {city or '(unknown)'}",
        "",
        f"Returned profile: {profile_url or '(no url)'}",
        "Profile's claimed entries:",
    ]
    judged = [
        c for c in claims if c.claim_type in ("education", "career_history",
                                              "current_employer", "current_title",
                                              "location")
    ]
    if not judged:
        lines.append("  (none)")
    for c in judged:
        lines.append(f"  - {c.claim_type}: {c.value}")
    return "\n".join(lines)


def _parse_verdict(text: str) -> LinkedInVerdict:
    """Parse the model's JSON object. Anything missing or malformed -> rejected:
    this gate may only pass a profile on an explicit, parseable 'verified'."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    parsed: object = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= start < end:
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                parsed = None
    if not isinstance(parsed, dict):
        return _REJECT_ON_ERROR

    decision = str(parsed.get("decision", "")).strip().lower()
    if decision not in _VALID_DECISIONS:
        return _REJECT_ON_ERROR
    try:
        confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    except (TypeError, ValueError):
        confidence = 0.0
    return LinkedInVerdict(decision, str(parsed.get("reason", "")).strip(), confidence)


def verify_linkedin_profile(
    client: Anthropic,
    name: str,
    *,
    profile_url: str,
    school: str,
    grad_year: int | None,
    roster_employer: str,
    city: str,
    claims: list[ClaimRow],
    model: str = HAIKU_MODEL,
    max_tokens: int = 512,
) -> tuple[LinkedInVerdict, int, int]:
    """Judge whether the agent's LinkedIn result is the roster person. Returns
    (verdict, haiku_in, haiku_out). Empty claims or any failure -> rejected
    with zero tokens (fail-closed)."""
    if not claims:
        return (
            LinkedInVerdict(DECISION_REJECTED, "agent returned no claims", 0.0),
            0,
            0,
        )

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{
                "role": "user",
                "content": _build_user(
                    name, profile_url, school, grad_year, roster_employer, city, claims
                ),
            }],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        tok_in = response.usage.input_tokens
        tok_out = response.usage.output_tokens
    except Exception:
        return _REJECT_ON_ERROR, 0, 0

    return _parse_verdict(text), tok_in, tok_out
