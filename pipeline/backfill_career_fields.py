"""Free, no-spend recompute of the DETERMINISTIC career fields on person_insights.

Born as a one-off backfill for a parser bug; now the shared "claims changed ->
refresh the derived row" step. The fields are pure functions of already-stored
claims, so recomputing costs nothing:

    first_employer            (Origins / "still at first firm")
    num_employers             (job-mobility proxy)
    years_to_md               (career velocity; re-gated on stored reached_md)
    still_first_firm /        (deterministic flags, recomputed only when
    started_sell_side          first_employer changed)
    current_role_start_year / (filled from a DATED open-ended career entry when
    tenure_years               PDL never provided one — LinkedIn refreshes add
                               exactly these dates)

The Haiku KPI flags and PDL attributes from the original run are preserved
untouched (re-running them would cost money).

Library use:   recompute_career_fields(conn, person_id)   # one person, commits NOT included
Script use:    python backfill_career_fields.py [--db data/titans.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import dataclasses
import sqlite3
from datetime import datetime, timezone

from career_analysis import (
    career_entries,
    first_post_grad_employer,
    num_employers,
    tenure_years,
    years_to_md,
)
from enrichment_store import ClaimRow
from kpi_classify import deterministic_flags
from grad_year import derive_grad_year
from person_insights_store import (
    all_person_insights,
    get_person_insight,
    init_person_insights_schema,
    upsert_person_insight,
)


def _claims_for(conn: sqlite3.Connection, person_id: int) -> list[ClaimRow]:
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


def _person_meta(conn: sqlite3.Connection, person_id: int) -> tuple[str, int | None]:
    row = conn.execute(
        "SELECT school, titan_class FROM people WHERE id = ?", (person_id,)
    ).fetchone()
    if row is None:
        return "", None
    return row["school"] or "", row["titan_class"]


def _current_role_start_from_claims(claims: list[ClaimRow]) -> int | None:
    """Start year of the current role per the claims themselves: the most recent
    DATED open-ended career entry (start set, end None = 'present'). Used only
    when PDL never supplied current_role_start_year."""
    starts = [
        e.start_year
        for e in career_entries(claims)
        if e.start_year is not None and e.end_year is None
    ]
    return max(starts) if starts else None


def recompute_career_fields(
    conn: sqlite3.Connection, person_id: int, *, dry_run: bool = False
) -> str | None:
    """Recompute one person's deterministic career fields from their claims.
    Returns a human-readable change summary, or None when nothing changed.
    Writes via upsert (caller commits); dry_run computes the summary only."""
    ins = get_person_insight(conn, person_id)
    if ins is None:
        return None
    claims = _claims_for(conn, person_id)
    school, titan_class = _person_meta(conn, person_id)
    edu_texts = [
        f"{c.value} {c.quote}".strip() for c in claims if c.claim_type == "education"
    ]
    gy = derive_grad_year(school, titan_class, edu_texts)

    new_first = first_post_grad_employer(claims, gy.year)
    new_nemp = num_employers(claims)
    new_ytm = years_to_md(claims, gy.year) if ins.reached_md else None

    # still_first_firm and started_sell_side are pure functions of
    # first_employer (+ current employer / sell-side names) — when first
    # changes, recompute both deterministically so the row stays consistent.
    # The three first-employer-independent Haiku flags are left untouched.
    new_sff = ins.still_first_firm
    new_sss = ins.started_sell_side
    if new_first != ins.first_employer:
        det = deterministic_flags(claims, new_first)
        new_sff = det.still_first_firm
        new_sss = det.started_sell_side

    # PDL is the authority on current_role_start_year when it matched; when it
    # never did, a dated open-ended career entry (the LinkedIn refresh payoff)
    # fills the gap so tenure stops reading as unknown.
    new_start = ins.current_role_start_year
    new_tenure = ins.tenure_years
    if new_start is None:
        claimed = _current_role_start_from_claims(claims)
        if claimed is not None:
            new_start = claimed
            new_tenure = tenure_years(claimed, datetime.now(timezone.utc).year)

    if (
        new_first == ins.first_employer
        and new_nemp == ins.num_employers
        and new_ytm == ins.years_to_md
        and new_start == ins.current_role_start_year
        and new_tenure == ins.tenure_years
    ):
        return None

    parts = []
    if new_first != ins.first_employer:
        parts.append(f"first '{ins.first_employer or '?'}' -> '{new_first or '?'}'")
    if new_nemp != ins.num_employers:
        parts.append(f"nemp {ins.num_employers} -> {new_nemp}")
    if new_ytm != ins.years_to_md:
        parts.append(f"ytm {ins.years_to_md} -> {new_ytm}")
    if new_sff != ins.still_first_firm:
        parts.append(f"first-firm {ins.still_first_firm} -> {new_sff}")
    if new_start != ins.current_role_start_year:
        parts.append(f"role-start {ins.current_role_start_year} -> {new_start}")
    if new_tenure != ins.tenure_years:
        parts.append(f"tenure {ins.tenure_years} -> {new_tenure}")
    summary = "; ".join(parts)

    if not dry_run:
        upsert_person_insight(
            conn,
            dataclasses.replace(
                ins,
                first_employer=new_first,
                num_employers=new_nemp,
                years_to_md=new_ytm,
                still_first_firm=new_sff,
                started_sell_side=new_sss,
                current_role_start_year=new_start,
                tenure_years=new_tenure,
            ),
        )
    return summary


def backfill(db_path: str, dry_run: bool) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_person_insights_schema(conn)

    insights = all_person_insights(conn)
    changed = 0
    for ins in insights:
        summary = recompute_career_fields(conn, ins.person_id, dry_run=dry_run)
        if summary:
            changed += 1
            print(f"  #{ins.person_id}: {summary}")

    if not dry_run:
        conn.commit()
    conn.close()
    verb = "would update" if dry_run else "updated"
    print(f"\nBackfill {verb} {changed}/{len(insights)} person_insights rows.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/titans.db")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    backfill(args.db, args.dry_run)


if __name__ == "__main__":
    main()
