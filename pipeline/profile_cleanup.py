"""Deterministic profile cleanup — runs AFTER the LLM reconciler + casing digest,
right before claims are persisted.

The reconciler's system prompt already asks for "one primary current role" and
"one fact per distinct real-world role", but it is probabilistic and the class-3
QA audit caught three recurring failures it let through:

1. Multiple "current" roles — two current_employer/current_title claims, or two
   open-ended career entries (Kimberly: Director + Real Assets Mgr both "present";
   Shaun: SonarSource + Enverus both flagged current). We must surface ONE.
2. Non-professional entries polluting career_history — student investment programs
   (Titans of Investing, Tanner Fund), personal/fan sites (UltimateDBZ.com), and
   volunteer/charity board seats inflate num_employers and can become the "first
   employer".
3. Title hygiene — a current_title that is really just the employer name ("U.S.
   Army", a division), which reads as "no real title".

These are deterministic, so we fix them with code rather than more prompt-tuning.
Every function is pure and never raises; an unparseable entry is left untouched.
"""
from __future__ import annotations

from career_analysis import _norm_company, parse_career_entry
from enrichment_store import ClaimRow

# Career entries whose company OR title contains one of these is a student
# program, academic fund, or personal project — not professional employment.
# High-precision: only clear, well-known non-jobs. Extend as the audit surfaces more.
NONPROFESSIONAL_CAREER_KW = (
    "titans of investing",
    "tanner fund",
    "student investment",
    "investment club",
    "stock pitch",
    "mays business school",      # the school itself is not an employer
    "tippie college",
    "student managed",
    "ultimatedbz",
)

# Volunteer / charity / board signals. Board seats are real but belong in their own
# section, not employment history — they distort first-employer and mobility metrics.
VOLUNTEER_CAREER_KW = (
    "volunteer",
    "board member",
    "advisory board",
    "board of directors",
    "trustee",
    "deacon",
    "usher",
)


def is_nonprofessional_career(value: str, quote: str = "") -> bool:
    """True when a career_history entry is a student program, academic fund,
    personal site, or a volunteer/board seat — i.e. not professional employment."""
    blob = f"{value} {quote}".lower()
    return any(kw in blob for kw in NONPROFESSIONAL_CAREER_KW + VOLUNTEER_CAREER_KW)


def drop_nonprofessional_careers(claims: list[ClaimRow]) -> list[ClaimRow]:
    """Remove career_history rows that are not professional employment. Leaves
    every other claim type untouched."""
    return [
        c
        for c in claims
        if not (
            c.claim_type == "career_history"
            and is_nonprofessional_career(c.value, c.quote)
        )
    ]


def _open_ended(entry) -> bool:
    """A role with a start year and no end year is ongoing ('present')."""
    return entry.start_year is not None and entry.end_year is None


def _currency_rank(entry) -> tuple:
    """Sort key for 'how current is this role': open-ended beats dated; then later
    end year; then later start year. Higher tuple == more current."""
    return (
        1 if _open_ended(entry) else 0,
        entry.end_year or 0,
        entry.start_year or 0,
    )


def _anchor_company(claims: list[ClaimRow]) -> str:
    """Normalized company of the single most-current career entry, or '' when no
    career entry names a company. Used to break ties between competing current
    roles so the headline matches the timeline."""
    entries = [
        parse_career_entry(c.value, c.quote)
        for c in claims
        if c.claim_type == "career_history"
    ]
    dated = [e for e in entries if e.company]
    if not dated:
        return ""
    best = max(dated, key=_currency_rank)
    return _norm_company(best.company)


def dedupe_current_role(claims: list[ClaimRow]) -> list[ClaimRow]:
    """Collapse to at most ONE current_employer and ONE current_title. When several
    compete, keep the one matching the most-current career entry's company; else
    the highest-confidence claim. Order of all other claims is preserved."""
    emp = [c for c in claims if c.claim_type == "current_employer"]
    tit = [c for c in claims if c.claim_type == "current_title"]
    if len(emp) <= 1 and len(tit) <= 1:
        return claims

    anchor = _anchor_company(claims)

    def pick(rows: list[ClaimRow]) -> ClaimRow | None:
        if not rows:
            return None
        if anchor:
            matched = [r for r in rows if _norm_company(r.value) == anchor]
            if matched:
                return max(matched, key=lambda r: r.confidence)
        return max(rows, key=lambda r: r.confidence)

    keep_emp = pick(emp)
    keep_tit = max(tit, key=lambda r: r.confidence) if tit else None
    # Drop only the LOSING current_employer/current_title rows; the winners stay in
    # their original positions so claim order is otherwise unchanged.
    drop_ids = {
        id(c) for c in emp + tit if c is not keep_emp and c is not keep_tit
    }
    return [c for c in claims if id(c) not in drop_ids]


def clean_current_title(claims: list[ClaimRow]) -> list[ClaimRow]:
    """Drop a current_title that is really just the employer name (e.g. employer
    'U.S. Army' surfaced as the title) — it carries no role information."""
    employer = next(
        (c.value for c in claims if c.claim_type == "current_employer"), ""
    )
    emp_norm = _norm_company(employer)
    if not emp_norm:
        return claims
    return [
        c
        for c in claims
        if not (
            c.claim_type == "current_title" and _norm_company(c.value) == emp_norm
        )
    ]


def clean_profile(claims: list[ClaimRow]) -> list[ClaimRow]:
    """Run all deterministic cleanups in order: drop non-professional careers,
    collapse to a single current role, then strip an employer-as-title. Pure."""
    out = drop_nonprofessional_careers(list(claims))
    out = dedupe_current_role(out)
    out = clean_current_title(out)
    return out
