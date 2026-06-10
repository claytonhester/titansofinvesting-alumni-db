"""Deterministic roll-ups for the Phase-3 insights snapshot.

Everything here is measured by SQL GROUP BY over the `claims` grain — no model,
no spend. Given the same database it returns the same numbers every time, which
is exactly what an aggregate "state of the cohort" view needs. The ONE field
this module does NOT measure is the narrative prose; it ships a deterministic
templated fallback so a snapshot is always complete, and the orchestrator may
swap in a Haiku-written narrative (the only billed part of Phase 3) on top.

Seniority is the one place a title has to be mapped onto a fixed ladder. We do
it with an ordered keyword classifier here (free, deterministic) as the default;
the orchestrator can pass an LLM classifier instead. The ladder itself
(SENIORITY_TIERS) is owned by insights_store so the web and pipeline agree on
the buckets.

Claim types this reads (see structuring.py / enrichment_store.py):
- current_employer -> landing firms
- current_title    -> current titles + seniority ladder
"""
from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import replace

from insights_store import (
    SENIORITY_TIERS,
    SENIORITY_UNKNOWN,
    FirmCount,
    InsightsSnapshot,
    SeniorityTier,
    SignatureStat,
    TitleCount,
    is_sample_for,
)

# A title is mapped onto the FIRST ladder bucket whose keyword it contains.
# Order is the whole game: more-specific / easily-confused forms are tested
# before the generic ones, so "vice president" never falls into the standalone
# "president" (C-suite) rule, and "chief …" outranks an accompanying "director".
# Each rule maps to a SENIORITY_TIERS index so the labels never drift from the
# store's canonical ladder.
_KEYWORD_RULES: tuple[tuple[tuple[str, ...], int], ...] = (
    # C-suite via "chief …"/CxO/owner — these never collide with the VP rule.
    (
        (
            "chief executive", "chief financial", "chief operating",
            "chief investment", "chief technology", "chief marketing",
            "chief information", "chief",
            # Bare CxO abbreviations must be space/comma-bounded — otherwise
            # "cto" matches inside "dire(cto)r" and sweeps every director into
            # C-suite. The text is space-padded, so these catch standalone forms.
            " ceo", "ceo ", "ceo,", " cfo", "cfo ", "cfo,", " coo", "coo ",
            "coo,", " cto", "cto ", "cto,", " cio", "cio ", "cio,", " cmo",
            "cmo ", "cmo,", "owner", "proprietor",
        ),
        4,
    ),
    # VP / Principal — tested before standalone "president" on purpose.
    (
        (
            "vice president", "vice-president", "svp", "evp", "v.p.", " vp",
            "vp ", "vp,", "principal",
        ),
        1,
    ),
    # Partner / Founder — before Director so "managing partner" wins over a
    # nearby "managing director", and "founding partner" lands here.
    (
        ("founder", "cofounder", "co-founder", "managing partner",
         "general partner", "partner"),
        3,
    ),
    # Director / Managing Director — generic leadership rung.
    (("managing director", "executive director", "director", "head of"), 2),
    # Remaining C-suite / ownership signals that aren't "chief".
    (("president", "chairman", "chairwoman", "chairperson", "chair of"), 4),
    # Entry rung last; only reached when nothing more senior matched.
    (("analyst", "associate", "intern", "trainee", "fellow", "advisor"), 0),
)

# How many ranked rows the web shows for the firm / title lists. Seniority is
# always computed over ALL titles regardless of these display caps.
DEFAULT_TOP_FIRMS = 12
DEFAULT_TOP_TITLES = 12


def people_total(conn: sqlite3.Connection) -> int:
    """Size of the whole cohort (the Stage-1 `people` table)."""
    row = conn.execute("SELECT COUNT(*) AS n FROM people").fetchone()
    return int(row["n"]) if row else 0


def enriched_count(conn: sqlite3.Connection) -> int:
    """How many people have AT LEAST ONE claim — the real denominator behind
    every aggregate. This is what coverage / is_sample are judged against."""
    row = conn.execute(
        "SELECT COUNT(DISTINCT person_id) AS n FROM claims"
    ).fetchone()
    return int(row["n"]) if row else 0


def _value_counts(conn: sqlite3.Connection, claim_type: str) -> list[tuple[str, int]]:
    """(value, count) for one claim_type, most common first. Values are trimmed
    and empty ones dropped so a stray blank claim can't top the chart."""
    rows = conn.execute(
        """
        SELECT TRIM(value) AS v, COUNT(*) AS n
        FROM claims
        WHERE claim_type = ? AND TRIM(value) != ''
        GROUP BY TRIM(value)
        ORDER BY n DESC, v ASC
        """,
        (claim_type,),
    ).fetchall()
    return [(r["v"], int(r["n"])) for r in rows]


def landing_firms(
    conn: sqlite3.Connection, top: int = DEFAULT_TOP_FIRMS
) -> tuple[FirmCount, ...]:
    """Most common current employers — where alumni have actually landed."""
    counts = _value_counts(conn, "current_employer")
    return tuple(FirmCount(company=v, count=n) for v, n in counts[:top])


def current_titles(
    conn: sqlite3.Connection, top: int = DEFAULT_TOP_TITLES
) -> tuple[TitleCount, ...]:
    """Most common current titles, verbatim, for the display list."""
    counts = _value_counts(conn, "current_title")
    return tuple(TitleCount(title=v, count=n) for v, n in counts[:top])


def clean_title_basic(title: str) -> str:
    """Free, deterministic title tidy-up: strip the employer / product / team /
    department suffix that follows a dash, an ' at ', or a comma, so raw titles
    like "Assistant Professor, Department of Pathology" or
    "... Sales Leader ... - IBM UKI Data Platforms" lose the noise that makes
    every one unique. Conservative — it only trims trailing context after a
    delimiter; it never collapses a role's function word (that's the Haiku
    canonicalizer's job). Returns the original (trimmed) when no delimiter is
    present. This is the fallback when the LLM omits a title."""
    t = (title or "").strip()
    if not t:
        return ""
    # Cut at the first place-/product-/department-naming delimiter.
    for sep in (" - ", " – ", " — ", " @ "):
        if sep in t:
            t = t.split(sep, 1)[0].strip()
    # " at <Employer>" (word boundary, case-insensitive).
    m = re.search(r"\bat\b", t, flags=re.IGNORECASE)
    if m and m.start() > 0:
        t = t[: m.start()].strip()
    # A trailing ", <department/specialty>" clause.
    if "," in t:
        t = t.split(",", 1)[0].strip()
    return t or (title or "").strip()


def classify_seniority_keyword(title: str) -> str:
    """Map a raw title onto the fixed ladder by ordered keyword match. Returns a
    SENIORITY_TIERS label, or SENIORITY_UNKNOWN when nothing matches — never an
    invented bucket. Free and deterministic; the LLM classifier is opt-in."""
    text = f" {title.lower().strip()} "
    for keywords, tier_index in _KEYWORD_RULES:
        if any(kw in text for kw in keywords):
            return SENIORITY_TIERS[tier_index]
    return SENIORITY_UNKNOWN


def seniority_breakdown(
    title_counts: Sequence[tuple[str, int]],
    classifier: Callable[[str], str] = classify_seniority_keyword,
) -> tuple[SeniorityTier, ...]:
    """Fold (title, count) pairs onto the ladder using `classifier`. Tiers are
    returned in the store's canonical career order; Unknown (if any) trails last
    so the web renders shallow→senior without re-sorting. Empty tiers are
    dropped."""
    totals: dict[str, int] = {}
    for title, count in title_counts:
        tier = classifier(title)
        totals[tier] = totals.get(tier, 0) + count

    ordered: list[SeniorityTier] = [
        SeniorityTier(tier=tier, count=totals[tier])
        for tier in SENIORITY_TIERS
        if totals.get(tier)
    ]
    if totals.get(SENIORITY_UNKNOWN):
        ordered.append(SeniorityTier(SENIORITY_UNKNOWN, totals[SENIORITY_UNKNOWN]))
    return tuple(ordered)


def founders_partners_count(seniority: Sequence[SeniorityTier]) -> int:
    """Headcount in the two most-senior buckets — the "made partner / founded or
    runs the firm" figure the scorecard headlines."""
    senior = {"Partner / Founder", "C-suite / Owner"}
    return sum(s.count for s in seniority if s.tier in senior)


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def signature_stats(
    *,
    people: int,
    enriched: int,
    firms: Sequence[FirmCount],
    distinct_employers: int,
    founders_partners: int,
) -> tuple[SignatureStat, ...]:
    """A small fixed set of headline numbers, all derived from the measured
    roll-ups above. Deterministic; recomputed identically on every run."""
    stats: list[SignatureStat] = [
        SignatureStat(
            label="Enriched so far",
            value=f"{enriched} / {people}",
            detail="alumni with verified profile data",
            pct=_pct(enriched, people),
        ),
    ]
    if firms:
        top = firms[0]
        stats.append(
            SignatureStat(
                label="Top landing firm",
                value=top.company,
                detail=f"{top.count} alumni",
                pct=_pct(top.count, enriched),
            )
        )
    stats.append(
        SignatureStat(
            label="Partners, founders & C-suite",
            value=str(founders_partners),
            detail="reached the senior tiers",
            pct=_pct(founders_partners, enriched),
        )
    )
    stats.append(
        SignatureStat(
            label="Distinct employers",
            value=str(distinct_employers),
            detail="unique current firms on record",
            pct=0,
        )
    )
    return tuple(stats)


def templated_narrative(
    *,
    people: int,
    enriched: int,
    firms: Sequence[FirmCount],
    founders_partners: int,
) -> str:
    """Deterministic prose over the measured numbers — the always-available
    fallback when the Haiku narrator is not run. States only what the roll-ups
    show; invents nothing."""
    if enriched == 0:
        return (
            f"The cohort spans {people} Titans of Investing alumni. None have "
            "been enriched yet, so aggregate insights are not available."
        )
    firm_names = ", ".join(f.company for f in firms[:3])
    lead = (
        f"Of {people} Titans of Investing alumni, {enriched} have verified "
        f"profile data so far."
    )
    where = (
        f" The most common landing firms include {firm_names}." if firm_names else ""
    )
    senior = (
        f" {founders_partners} have reached partner, founder, or C-suite roles."
        if founders_partners
        else ""
    )
    return lead + where + senior


def build_snapshot(
    conn: sqlite3.Connection,
    snapshot_year: int,
    *,
    classifier: Callable[[str], str] = classify_seniority_keyword,
    top_firms: int = DEFAULT_TOP_FIRMS,
    top_titles: int = DEFAULT_TOP_TITLES,
) -> InsightsSnapshot:
    """Assemble the full deterministic snapshot for one year. The narrative is
    the templated fallback and haiku tokens are zero — the orchestrator overlays
    an LLM narrative/seniority via dataclasses.replace when --llm is set. The
    is_sample flag is decided here from real coverage, so the web's real-vs-
    illustrative gate is honest no matter how the snapshot was produced."""
    people = people_total(conn)
    enriched = enriched_count(conn)
    coverage = enriched / people if people else 0.0

    firms = landing_firms(conn, top_firms)
    all_title_counts = _value_counts(conn, "current_title")
    titles = tuple(
        TitleCount(title=v, count=n) for v, n in all_title_counts[:top_titles]
    )
    seniority = seniority_breakdown(all_title_counts, classifier)
    founders = founders_partners_count(seniority)
    distinct_employers = len(_value_counts(conn, "current_employer"))

    narrative = templated_narrative(
        people=people, enriched=enriched, firms=firms, founders_partners=founders
    )
    stats = signature_stats(
        people=people,
        enriched=enriched,
        firms=firms,
        distinct_employers=distinct_employers,
        founders_partners=founders,
    )

    return InsightsSnapshot(
        snapshot_year=snapshot_year,
        people_total=people,
        enriched_count=enriched,
        coverage=coverage,
        is_sample=is_sample_for(enriched, people),
        narrative=narrative,
        landing_firms=firms,
        current_titles=titles,
        seniority=seniority,
        signature_stats=stats,
        founders_partners=founders,
    )


def with_llm_narrative(
    snap: InsightsSnapshot,
    *,
    narrative: str,
    seniority: tuple[SeniorityTier, ...] | None = None,
    current_titles: tuple[TitleCount, ...] | None = None,
    haiku_tokens_in: int = 0,
    haiku_tokens_out: int = 0,
) -> InsightsSnapshot:
    """Overlay the billed Haiku outputs onto a deterministic snapshot. Seniority
    is replaced only if the LLM reclassified it; current_titles is replaced only
    if the LLM canonicalized it (near-duplicate titles folded together);
    founders_partners is recomputed from whichever ladder is now in force so the
    headline stays consistent."""
    new_seniority = seniority if seniority is not None else snap.seniority
    new_titles = current_titles if current_titles is not None else snap.current_titles
    return replace(
        snap,
        narrative=narrative or snap.narrative,
        seniority=new_seniority,
        current_titles=new_titles,
        founders_partners=founders_partners_count(new_seniority),
        haiku_tokens_in=haiku_tokens_in,
        haiku_tokens_out=haiku_tokens_out,
    )
