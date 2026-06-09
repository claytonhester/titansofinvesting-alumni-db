"""Haiku classifier for the four cohort KPIs, per person.

The Overview scorecard headlines four signals about each alumnus:
    on_buy_side       in an investing seat now (vs. a bank/consulting/corporate)
    reached_md        ever reached Managing Director / Partner / C-suite or above
    founder_partner   runs their own fund or holds a partner / GP seat
    still_first_firm  current employer is still their first post-grad employer

A single Haiku call per person reads the assembled résumé (current role + work
history) plus the deterministic grad_year and first_employer, and returns the
four booleans. `reached_md` records whether they have EVER reached that bar — the
"10 years after graduation" fairness rule is applied later, at roll-up time, over
grad_year; the classifier only states the per-person truth.

Never raises: on any model/parse failure it returns a deterministic keyword-based
classification computed from the same claims, so a bulk loop degrades gracefully
instead of dropping a person. That deterministic result is also the value used
when there is no Anthropic client at all.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from career_analysis import career_entries
from enrichment_store import ClaimRow
from insights_rollup import SENIORITY_TIERS, classify_seniority_keyword
from structuring import HAIKU_MODEL

MODEL_METHOD = "haiku-kpi"

# Keywords (already lowercased) for the deterministic fallback.
_FOUNDER_PARTNER_KW = (
    "founder", "co-founder", "cofounder", "managing partner", "general partner",
    "founding partner", "partner", "owner", "proprietor", "principal",
)
_BUY_SIDE_EMPLOYER_KW = (
    "capital", "partners", "ventures", "equity", "asset management", "investments",
    "investment management", "advisors", "advisers", "fund", "hedge", "holdings",
    "management", "wealth", "private equity", "venture",
)
_BUY_SIDE_TITLE_KW = (
    "portfolio manager", "investor", "investment", "analyst", "principal",
    "managing director", "partner", "associate",
)
# Titles/employers that are clearly NOT buy-side even if a generic word matches.
_SELL_SIDE_KW = (
    "consultant", "consulting", "audit", "tax", "advisory services", "accountant",
)


@dataclass(frozen=True)
class KpiFlags:
    on_buy_side: bool
    reached_md: bool
    founder_partner: bool
    still_first_firm: bool


_SYSTEM = """You classify one finance/investing professional — an alumnus of a \
Texas university program (Titans of Investing) — onto four career signals, using \
ONLY the facts given. Do not invent or assume beyond them.

Return each as a strict boolean:
- on_buy_side: their CURRENT role is a buy-side investing seat (asset management, \
private equity, venture capital, hedge fund, family office, portfolio management, \
a pension/endowment investor, or running their own fund). FALSE for sell-side \
banking, consulting, accounting/audit, corporate operating roles, or law.
- reached_md: they have EVER reached Managing Director, Director, Partner, \
Principal (at an investment firm), C-suite (CEO/CFO/CIO/COO), or owner/founder — \
at any point in their history, current or past. This is "did they ever clear the \
senior bar", regardless of when.
- founder_partner: they currently run their own firm/fund OR hold a partner / \
general-partner / managing-partner seat (not merely a salaried 'partner'-in-name \
title at a Big Four firm — judge by substance when you can).
- still_first_firm: their current employer is the SAME organization as their \
first post-grad employer given below (allow for renames/acquisitions).

Return ONLY this JSON object, nothing else:
{"on_buy_side": bool, "reached_md": bool, "founder_partner": bool, \
"still_first_firm": bool}"""


def _field(claims: list[ClaimRow], claim_type: str) -> str:
    for c in claims:
        if c.claim_type == claim_type and c.value.strip():
            return c.value.strip()
    return ""


def _all_titles(claims: list[ClaimRow]) -> list[str]:
    titles = [_field(claims, "current_title")]
    titles += [e.title for e in career_entries(claims) if e.title]
    return [t for t in titles if t]


def _peak_index(titles: list[str]) -> int:
    """Highest seniority-ladder index across all titles, or -1 if none map."""
    best = -1
    for t in titles:
        label = classify_seniority_keyword(t)
        if label in SENIORITY_TIERS:
            best = max(best, SENIORITY_TIERS.index(label))
    return best


def _norm_firm(name: str) -> str:
    return " ".join(name.lower().replace(",", " ").split())


def deterministic_flags(
    claims: list[ClaimRow], first_employer: str
) -> KpiFlags:
    """Keyword/seniority-based classification — the always-available fallback."""
    titles = _all_titles(claims)
    title_blob = " ".join(titles).lower()
    cur_title = _field(claims, "current_title").lower()
    cur_employer = _field(claims, "current_employer").lower()

    peak = _peak_index(titles)
    reached_md = peak >= 2  # Director / Managing Director or above
    founder_partner = any(kw in title_blob for kw in _FOUNDER_PARTNER_KW)

    employer_buy = any(kw in cur_employer for kw in _BUY_SIDE_EMPLOYER_KW)
    title_buy = any(kw in cur_title for kw in _BUY_SIDE_TITLE_KW)
    sell = any(kw in cur_title or kw in cur_employer for kw in _SELL_SIDE_KW)
    on_buy_side = (employer_buy or title_buy) and not sell

    still_first = bool(first_employer) and bool(cur_employer) and (
        _norm_firm(first_employer) == _norm_firm(cur_employer)
    )
    return KpiFlags(on_buy_side, reached_md, founder_partner, still_first)


def _build_user(
    name: str, grad_year: int | None, first_employer: str, claims: list[ClaimRow]
) -> str:
    lines = [
        f"Name: {name}",
        f"Graduation year: {grad_year if grad_year is not None else '(unknown)'}",
        f"First post-grad employer: {first_employer or '(unknown)'}",
        f"Current title: {_field(claims, 'current_title') or '(unknown)'}",
        f"Current employer: {_field(claims, 'current_employer') or '(unknown)'}",
        "",
        "Work history:",
    ]
    entries = [c for c in claims if c.claim_type == "career_history"]
    if entries:
        for c in entries:
            lines.append(f"  - {c.value}")
    else:
        lines.append("  (none on record)")
    return "\n".join(lines)


def _parse(text: str) -> dict | None:
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


def _coerce(obj: dict, key: str, fallback: bool) -> bool:
    """A present, real boolean wins; anything missing/odd falls back."""
    val = obj.get(key)
    return val if isinstance(val, bool) else fallback


def classify_kpis(
    client: Anthropic | None,
    name: str,
    grad_year: int | None,
    first_employer: str,
    claims: list[ClaimRow],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 256,
) -> tuple[KpiFlags, int, int]:
    """Classify the four KPIs for one person. Returns (flags, haiku_in, haiku_out).
    With no client, or on any failure, returns the deterministic fallback and zero
    token usage. A successful call fills any missing/odd field from that fallback,
    so the result is always complete."""
    fallback = deterministic_flags(claims, first_employer)
    if client is None:
        return fallback, 0, 0

    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _build_user(name, grad_year, first_employer, claims)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
        tok_in = response.usage.input_tokens
        tok_out = response.usage.output_tokens
    except Exception:
        return fallback, 0, 0

    obj = _parse(text)
    if obj is None:
        return fallback, tok_in, tok_out

    flags = KpiFlags(
        on_buy_side=_coerce(obj, "on_buy_side", fallback.on_buy_side),
        reached_md=_coerce(obj, "reached_md", fallback.reached_md),
        founder_partner=_coerce(obj, "founder_partner", fallback.founder_partner),
        still_first_firm=_coerce(obj, "still_first_firm", fallback.still_first_firm),
    )
    return flags, tok_in, tok_out
