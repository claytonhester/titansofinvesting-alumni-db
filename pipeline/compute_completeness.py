"""Deterministic profile-completeness score (0-100) per enriched person.

The quality problems this catches were all found by accident before it existed:
the identity review queue rotted unnoticed for weeks, and the Bart Howe case
(rich-looking profile, stale undated roles) only surfaced because a human put
his page next to his LinkedIn. This makes the system grade every profile after
each enrichment batch so weak ones raise their own hand.

Score components (sum to 100):

    20  current role        current_employer AND current_title both present
    15  education           >= 1 education claim
    25  career history      scaled by entry count up to 3 (1 entry=9, 2=17, 3+=25)
    10  bio                 short_bio of meaningful length
    10  press               >= 1 news_mention claim
    10  linkedin            a LinkedIn URL among public_links
    10  dated careers       share of career entries carrying a year range
                            (the "Bart detector": titles without dates score 0 here)

Pure arithmetic over claims already in the DB — no API calls, no spend. Writes
ONLY the person_insights.completeness_score column (owned by this script; the
enrichment upsert deliberately excludes it). People with zero claims score 0.

Usage:  python compute_completeness.py [--db data/titans.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from career_analysis import parse_career_entry
from config import DB_PATH
from db import connect
from deep_search_flag import should_flag_for_deep_search
from directory_hosts import registrable_host
from enrichment_store import ClaimRow
from person_insights_store import init_person_insights_schema

# Component weights — must sum to 100 (asserted below so a future tweak that
# unbalances the scale fails loudly at import time).
W_CURRENT_ROLE = 20
W_EDUCATION = 15
W_CAREER = 25
W_BIO = 10
W_PRESS = 10
W_LINKEDIN = 10
W_DATED = 10
assert (
    W_CURRENT_ROLE + W_EDUCATION + W_CAREER + W_BIO + W_PRESS + W_LINKEDIN + W_DATED
    == 100
)

# A bio shorter than this is a fragment, not a biography.
MIN_BIO_CHARS = 50

# Full career credit at this many entries; fewer earn a proportional share.
FULL_CAREER_ENTRIES = 3


@dataclass(frozen=True)
class CompletenessBreakdown:
    """Score plus the component facts, for explainable reporting."""
    score: int
    has_current_role: bool
    has_education: bool
    career_entries: int
    has_bio: bool
    has_press: bool
    has_linkedin: bool
    dated_career_share: float  # 0.0-1.0 over career entries (0.0 when none)


def _is_linkedin_url(url: str) -> bool:
    """True only for a PROFILE URL (linkedin.com/in/<slug>). A /posts/ or
    /pulse/ link is a mention ABOUT the person, not their profile — counting it
    gave LinkedIn credit to people whose actual profile was never found."""
    return registrable_host(url) == "linkedin.com" and "/in/" in (url or "")


def _career_entry_dated(claim: ClaimRow) -> bool:
    entry = parse_career_entry(claim.value, claim.quote or "")
    return entry.start_year is not None or entry.end_year is not None


def compute_breakdown(claims: list[ClaimRow]) -> CompletenessBreakdown:
    """Pure scoring over one person's claims. Empty claims -> score 0."""
    by_type: dict[str, list[ClaimRow]] = {}
    for c in claims:
        by_type.setdefault(c.claim_type, []).append(c)

    has_current_role = bool(by_type.get("current_employer")) and bool(
        by_type.get("current_title")
    )
    has_education = bool(by_type.get("education"))
    career = by_type.get("career_history", [])
    has_bio = any(
        len((c.value or "").strip()) >= MIN_BIO_CHARS
        for c in by_type.get("short_bio", [])
    )
    has_press = bool(by_type.get("news_mention"))
    # A LinkedIn URL can arrive as a public_links claim OR as the dedicated
    # linkedin_url claim the search-resolver records — count either.
    has_linkedin = any(
        _is_linkedin_url(c.source_url) or _is_linkedin_url(c.value)
        for c in by_type.get("public_links", []) + by_type.get("linkedin_url", [])
    )
    dated = sum(1 for c in career if _career_entry_dated(c))
    dated_share = (dated / len(career)) if career else 0.0

    score = 0
    score += W_CURRENT_ROLE if has_current_role else 0
    score += W_EDUCATION if has_education else 0
    score += round(W_CAREER * min(len(career), FULL_CAREER_ENTRIES) / FULL_CAREER_ENTRIES)
    score += W_BIO if has_bio else 0
    score += W_PRESS if has_press else 0
    score += W_LINKEDIN if has_linkedin else 0
    score += round(W_DATED * dated_share)

    return CompletenessBreakdown(
        score=score,
        has_current_role=has_current_role,
        has_education=has_education,
        career_entries=len(career),
        has_bio=has_bio,
        has_press=has_press,
        has_linkedin=has_linkedin,
        dated_career_share=dated_share,
    )


def _load_claims(conn: sqlite3.Connection, person_id: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    return [
        ClaimRow(
            claim_type=r["claim_type"],
            value=r["value"],
            source_url=r["source_url"],
            quote=r["quote"],
            confidence=r["confidence"],
            extraction_method=r["extraction_method"],
        )
        for r in rows
    ]


def recompute_completeness(
    conn: sqlite3.Connection, person_id: int
) -> CompletenessBreakdown:
    """Compute + persist one person's score AND deep-search flag. Caller commits."""
    breakdown = compute_breakdown(_load_claims(conn, person_id))
    needs_deep, reason = should_flag_for_deep_search(breakdown)
    # A person already processed by the deep pass never re-flags (queue drains).
    done = conn.execute(
        "SELECT deep_search_done FROM person_insights WHERE person_id = ?",
        (person_id,),
    ).fetchone()
    if done and done[0]:
        needs_deep, reason = False, ""
    conn.execute(
        "UPDATE person_insights SET completeness_score = ?, "
        "needs_deep_search = ?, deep_search_reason = ? WHERE person_id = ?",
        (breakdown.score, 1 if needs_deep else 0, reason, person_id),
    )
    return breakdown


def run(db_path: str, dry_run: bool) -> int:
    with connect(Path(db_path)) as conn:
        init_person_insights_schema(conn)  # additive migration for the column
        people = conn.execute(
            "SELECT pi.person_id, p.full_name, pi.completeness_score, "
            "pi.deep_search_done "
            "FROM person_insights pi JOIN people p ON p.id = pi.person_id "
            "ORDER BY pi.person_id"
        ).fetchall()

        changed = 0
        low: list[tuple[int, str, int]] = []
        flagged: list[tuple[int, str, int, str]] = []
        for row in people:
            breakdown = compute_breakdown(_load_claims(conn, row["person_id"]))
            needs_deep, reason = should_flag_for_deep_search(breakdown)
            # Already deep-passed → never re-flag (the queue drains).
            if row["deep_search_done"]:
                needs_deep, reason = False, ""
            if breakdown.score != (row["completeness_score"] or 0):
                changed += 1
            # Always write score + flag: the flag is set AND cleared every run, so
            # a profile that became rich in the deep pass clears itself here.
            if not dry_run:
                conn.execute(
                    "UPDATE person_insights SET completeness_score = ?, "
                    "needs_deep_search = ?, deep_search_reason = ? "
                    "WHERE person_id = ?",
                    (breakdown.score, 1 if needs_deep else 0, reason, row["person_id"]),
                )
            if breakdown.score < 60:
                low.append((row["person_id"], row["full_name"], breakdown.score))
            if needs_deep:
                flagged.append((row["person_id"], row["full_name"], breakdown.score, reason))

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    label = "would update" if dry_run else "updated"
    print(f"Completeness: {label} {changed}/{len(people)} people")
    if low:
        print(f"{len(low)} people below 60 (refresh candidates):")
        for pid, name, score in sorted(low, key=lambda t: t[2]):
            print(f"  [{pid:>4}] {score:>3}  {name}")
    print(f"Deep-search queue: {len(flagged)}/{len(people)} flagged "
          f"(needs_deep_search=1) — run `phase2_enrich.py --needs-deep`")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument("--dry-run", action="store_true", help="report only, no writes")
    args = ap.parse_args(argv)
    return run(args.db, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
