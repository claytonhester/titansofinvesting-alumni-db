"""Cross-industry seniority ladder (v2) — sector-aware level classification.

The original ladder (insights_store.SENIORITY_TIERS) was built for a pure
investment-banking cohort: VP below MD, the word "director" auto-senior, and no
concept of "Manager" at all. That assumption breaks on a mixed cohort where a
Google "Director" is mid-level and a Google "VP" is senior — the exact inverse
of finance. It also dumped ~31% of real titles into "Unknown" because corporate
Manager / Portfolio-Manager / Consultant tracks contain none of its keywords.

This module replaces that with a FOUR-rung cross-industry ladder plus an explicit
"Non-title" sink for department/team names and college/volunteer noise that were
never job titles in the first place:

    Entry / IC  ->  Manager  ->  Senior Leadership  ->  Executive / Founder

The two product thresholds read straight off the ladder:
    reached_manager           = peak level >= Manager           (index 1)
    reached_senior_leadership = peak level >= Senior Leadership  (index 2)

The hard part — that "VP"/"Director" mean opposite things in finance vs
corporate — is resolved by classifying each role WITH its employer, so the model
(or the deterministic fallback) can apply the right reading per sector. The
Haiku classifier is the primary path; the keyword fallback here keeps coverage
total and the module never raises.

Pure and deterministic except for `classify_levels`, which makes the one billed
Haiku call. Both paths only ever emit a label from LEVELS or NON_TITLE.
"""
from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from anthropic import Anthropic

# Career order, shallow -> senior. Index IS the rank; thresholds compare indices.
LEVELS: tuple[str, ...] = (
    "Entry / IC",            # 0  analyst, associate, intern, junior IC, coordinator
    "Manager",               # 1  manager, sr manager, PM, finance-VP, corp-director
    "Senior Leadership",     # 2  finance-MD/Director/Head, corp-VP+, partner, principal(svc)
    "Executive / Founder",   # 3  C-suite, president, founder/owner, managing/general partner
)
NON_TITLE = "Non-title"      # dept/team names, college/sports/volunteer, rank-less noise

LEVEL_INDEX = {label: i for i, label in enumerate(LEVELS)}
MANAGER_INDEX = LEVEL_INDEX["Manager"]
SENIOR_INDEX = LEVEL_INDEX["Senior Leadership"]

_ALLOWED = frozenset(LEVELS) | {NON_TITLE}

HAIKU_MODEL = "claude-haiku-4-5-20251001"

# ---------------------------------------------------------------------------
# Deterministic fallback — sector-aware keyword reading.
#
# Only used when Haiku omits or mis-labels a role. It infers "is this a finance
# employer?" from the company name, then applies the correct reading of the
# ambiguous words (VP, Director) for that side. Everything unambiguous (analyst,
# chief, founder, manager) reads the same on both sides.
# ---------------------------------------------------------------------------

# Tokens that strongly signal a financial-services employer. Word-ish substrings;
# the employer is lower-cased and space-padded before testing.
_FINANCE_EMPLOYER_KW: tuple[str, ...] = (
    "capital", "partners", "advisor", "advisors", "securities", "asset",
    "asset management", "investment", "investments", "equity", "ventures",
    "fund", "funds", "holdings", "bancorp", " bank", "bank ", "financial",
    "wealth", "trust", "sachs", "morgan", "stanley", "citadel", "blackstone",
    "kkr", "carlyle", "apollo", "tpg", "bain capital", "lazard", "evercore",
    "jefferies", "raymond james", "utimco", "retirement system",
    # Institutional asset owners — pension / endowment / sovereign / family office.
    # ("foundation" is deliberately omitted: too many non-investing nonprofits.)
    "pension", "endowment", "sovereign wealth", "family office",
)

# Non-title sinks: department/team labels and college/volunteer noise that the
# fallback should never award a rank to.
_NON_TITLE_KW: tuple[str, ...] = (
    "teammate", "line camp", "orientation leader", "lacrosse", "student-athlete",
    "intercambio", "graduate assistant", "mba candidate", "mba graduate",
    "doctoral student", "phd student", "graduate student", "law clerk",
    "student recruiter",
)
# Pure department / function strings (no rank, no person) seen contaminating the
# title field. Exact-ish match after normalisation.
_DEPT_LABELS: frozenset[str] = frozenset({
    "jpm alternatives", "investment & development", "investments", "investment",
    "private markets", "private equity", "global structuring", "strategic finance",
    "corporate development", "external public markets group", "emerging managers group",
    "gulf coast gas origination", "gulf coast gas trading and operations",
    "north east gas origination",
})

_FOUNDER_OWNER_KW = ("founder", "cofounder", "co-founder", "owner", "proprietor")
# Unambiguous executive signals. NOTE: "president" is handled separately and
# guarded against "vice president" — it must NOT live here, or every VP would be
# swept into the executive rung.
_EXEC_KW = (
    "chief", " ceo", "ceo ", "ceo,", " cfo", "cfo ", "cfo,", " coo", "coo ",
    "coo,", " cio", "cio ", "cio,", " cto", "cto ", "cto,", " cmo", "cmo ",
    "cmo,", "managing partner", "general partner",
)
# President / chairman count as executive ONLY when not part of "vice president".
_PRESIDENT_KW = ("president", "chairman", "chairwoman", "chairperson")
_VP_KW = (
    "vice president", "vice-president", " svp", "svp ", " evp", "evp ",
    " vp", "vp ", "vp,", "v.p.",
)
_PARTNER_KW = ("partner",)  # plain partner -> Senior Leadership (managing/general caught above)
_MANAGER_KW = (
    "manager", "head of", "portfolio manager", "investment manager",
)
_ENTRY_KW = (
    "analyst", "associate", "intern", "trainee", "fellow", "coordinator",
    "assistant ", "summer ", "junior ",
)


def _norm(s: str) -> str:
    return " ".join((s or "").lower().strip().split())


def _is_finance_employer(employer: str) -> bool:
    e = f" {_norm(employer)} "
    return any(kw in e for kw in _FINANCE_EMPLOYER_KW)


def classify_level_keyword(title: str, employer: str = "") -> str:
    """Sector-aware deterministic reading of one role. Order matters: the most
    senior unambiguous signals win first, then the sector-split words, then the
    entry rung; anything left is Non-title."""
    t = _norm(title)
    if not t:
        return NON_TITLE
    if t in _DEPT_LABELS:
        return NON_TITLE
    padded = f" {t} "
    if any(kw in padded for kw in _NON_TITLE_KW):
        return NON_TITLE

    has_vp = any(kw in padded for kw in _VP_KW)

    # Executive / Founder — chief/CxO/owner/founder/managing partner, plus
    # standalone president/chairman (but NEVER "vice president").
    if any(kw in padded for kw in _EXEC_KW) or any(kw in padded for kw in _FOUNDER_OWNER_KW):
        return LEVELS[3]
    if not has_vp and any(kw in padded for kw in _PRESIDENT_KW):
        return LEVELS[3]

    finance = _is_finance_employer(employer)

    # Managing Director / Executive Director — senior on both sides.
    if "managing director" in t or "executive director" in t:
        return LEVELS[2]

    # Plain partner -> Senior Leadership (managing/general already handled).
    if any(kw in padded for kw in _PARTNER_KW):
        return LEVELS[2]

    has_director = "director" in t  # already excluded MD/ED above
    has_principal = "principal" in t

    if finance:
        # Finance: VP / Principal = Manager (mid); Director / Head = Senior.
        if has_director or "head of" in t:
            return LEVELS[2]
        if has_vp or has_principal:
            return LEVELS[1]
    else:
        # Corporate: VP+ = Senior; Director / Manager = Manager (mid).
        if has_vp:
            return LEVELS[2]
        if has_director:
            return LEVELS[1]
        if has_principal:
            return LEVELS[2]  # corp "principal" (e.g. consulting) reads senior

    # Manager family (and finance "head of" handled above; corp head-of senior).
    if not finance and "head of" in t:
        return LEVELS[2]
    if any(kw in padded for kw in _MANAGER_KW):
        return LEVELS[1]

    # Entry rung.
    if any(kw in padded for kw in _ENTRY_KW):
        return LEVELS[0]

    # A real-looking role with no seniority signal: count as employed, not
    # senior. Conservative on purpose — never award Manager+ without evidence.
    return LEVELS[0]


def level_index(label: str) -> int | None:
    """Rank index for a ladder label; None for Non-title / unknown so callers
    can drop it from the career spine without it polluting peak/threshold math."""
    return LEVEL_INDEX.get(label)


# ---------------------------------------------------------------------------
# Haiku classifier — the primary path. One call over distinct (title, employer)
# pairs, sector inferred by the model from the employer name.
# ---------------------------------------------------------------------------

_SYSTEM = f"""You place each job ROLE onto a fixed cross-industry seniority ladder.

You are given a TITLE and the EMPLOYER it was held at. Use the employer to judge
the INDUSTRY, because the same word means different seniorities in different
industries.

Emit EXACTLY ONE label per role, copied verbatim:
- "Entry / IC"            (analyst, associate, intern, trainee, coordinator, junior individual contributor)
- "Manager"               (manager, senior manager, team lead, portfolio manager, investment manager)
- "Senior Leadership"     (the senior-leadership tier, ~1-2 rungs below the CEO)
- "Executive / Founder"   (C-suite, President, Chairman, Owner, Founder, Managing/General Partner)
- "Non-title"             (NOT a job title: a department/team/desk name, a pure function label
                           with no rank, or a college / sports / volunteer / student role)

INDUSTRY-DEPENDENT READING — this is the whole point:
- In FINANCE (investment banks, PE, VC, hedge funds, asset managers, wealth/advisory):
    * "Vice President" / "VP" / "SVP" / "EVP" / "Principal"  -> "Manager"  (mid-level in finance)
    * "Director" / "Executive Director" / "Managing Director" / "MD" / "Head of"  -> "Senior Leadership"
    * "Partner"  -> "Senior Leadership"   ("Managing Partner"/"General Partner" -> "Executive / Founder")
- INSTITUTIONAL ASSET OWNERS — public pension funds, retirement systems, university
  endowments, foundations, sovereign-wealth / family offices (e.g. "Teacher Retirement
  System", "UTIMCO", "A&M Foundation", "Terry Foundation", "X Endowment"). These use the
  FINANCE convention even though the employer name may not look like a bank, and the
  EMPLOYER overrides any sector hint:
    * "Director" / "Senior Director" / "Managing Director" / "Head of"  -> "Senior Leadership"
    * "Investment Manager" / "Senior Investment Manager" / "Portfolio Manager"  -> "Manager"
    * "Investment Analyst" / "Associate"  -> "Entry / IC"
    * "Chief Investment Officer" / "Deputy CIO"  -> "Executive / Founder"
- In CORPORATE / TECH / HEALTHCARE / CPG / GOVERNMENT / non-finance:
    * "Manager" / "Senior Manager" / "Director" / "Senior Director"  -> "Manager"  (mid-level in corporate)
    * "VP" / "SVP" / "EVP" / "General Manager (division)" / org-level "Head of"  -> "Senior Leadership"
    * "Partner"  -> "Senior Leadership"
- ACADEMIA: full "Professor" / "Dean" / "Department Chair" -> "Senior Leadership";
    "Assistant/Associate Professor" -> "Manager"; PhD student / postdoc / researcher / clerk -> "Entry / IC".

UNIVERSAL RULES:
- "Chief ___" / CEO / CFO / COO / CIO / CTO / President / Chairman / Owner / Founder -> "Executive / Founder".
- "Analyst" / "Associate" / "Intern" / "Trainee" / "Coordinator" -> "Entry / IC".
- A real role with NO seniority signal (e.g. "Investor", "Investment Professional",
  "Consultant" with no grade) -> "Entry / IC". Do NOT award Manager+ without evidence.
- A department/team/desk NAME ("JPM Alternatives", "Private Markets", "Global Structuring",
  "Corporate Development", "Strategic Finance"), or a college/sports/volunteer/student role
  ("Teammate", "Assistant Lacrosse Coach", "MBA Candidate", "Line Camp Leader") -> "Non-title".
- Never invent a label outside the five above.

Output ONLY a JSON object mapping each role id to its label. No prose."""


@dataclass(frozen=True)
class LevelClassification:
    """labels: {(title, employer) -> level label} for every input pair.
    Coverage is always total (fallback fills any gap). Carries token counts."""
    labels: dict[tuple[str, str], str]
    input_tokens: int
    output_tokens: int


def _parse_json(text: str) -> dict:
    try:
        start, end = text.index("{"), text.rindex("}")
        obj = json.loads(text[start : end + 1])
        return obj if isinstance(obj, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


def classify_levels(
    client: Anthropic,
    roles: Sequence[tuple[str, str, str]],
    *,
    model: str = HAIKU_MODEL,
    chunk_size: int = 120,
    max_tokens: int = 4096,
    fallback: Callable[[str, str], str] = classify_level_keyword,
) -> LevelClassification:
    """Classify distinct roles onto the ladder with batched Haiku calls.

    `roles` is a list of (title, employer, sector) triples. The sector is a hint
    we already know (person_insights.current_sector / first_sector) — passed so
    the model never has to GUESS finance-vs-corporate from the employer name when
    we can just tell it. The cache key is still (title, employer): the sector is
    a function of the employer, so it only sharpens the same call.

    Any role the model omits or labels off-ladder falls back to the deterministic
    reader, so coverage is always total and the result can never hold an invented
    label. Temperature 0 keeps a given role's label reproducible across runs.

    No network work for empty input. Never raises: a failed/garbled call degrades
    that whole chunk to the fallback."""
    # Dedup on (title, employer); keep the first non-empty sector hint seen.
    hint: dict[tuple[str, str], str] = {}
    for t, e, s in roles:
        key = (_norm(t), _norm(e))
        if not key[0]:
            continue
        if key not in hint or (not hint[key] and _norm(s)):
            hint[key] = _norm(s)
    distinct = sorted(hint)
    if not distinct:
        return LevelClassification({}, 0, 0)

    labels: dict[tuple[str, str], str] = {}
    tok_in = tok_out = 0

    for start in range(0, len(distinct), chunk_size):
        chunk = distinct[start : start + chunk_size]
        ids = {f"r{i}": pair for i, pair in enumerate(chunk)}
        lines = "\n".join(
            f'- {rid}: title="{t}" employer="{e or "unknown"}"'
            + (f' sector="{hint[(t, e)]}"' if hint.get((t, e)) else "")
            for rid, (t, e) in ids.items()
        )
        user = (
            "Classify each role. Return a JSON object mapping each id to its "
            'label, e.g. {"r0": "Senior Leadership"}.\n\nRoles:\n' + lines
        )
        mapping: dict = {}
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": user}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            mapping = _parse_json(text)
            tok_in += resp.usage.input_tokens
            tok_out += resp.usage.output_tokens
        except Exception:
            mapping = {}  # whole chunk degrades to fallback below

        for rid, pair in ids.items():
            proposed = mapping.get(rid)
            if isinstance(proposed, str) and proposed in _ALLOWED:
                labels[pair] = proposed
            else:
                labels[pair] = fallback(pair[0], pair[1])

    return LevelClassification(labels=labels, input_tokens=tok_in, output_tokens=tok_out)
