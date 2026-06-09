"""Free, no-spend backfill of the career fields broken by the parser bug.

The reconciler writes career_history values as "Title, Company (years)" (comma-
separated), but an earlier parse_career_entry only split on " at " — so company
came back empty and three DETERMINISTIC person_insights fields were wrong:

    first_employer   (Origins / "still at first firm")
    num_employers    (job-mobility proxy)
    years_to_md      (career velocity)

Those are pure functions of the already-stored claims, so we can recompute them
in place WITHOUT re-spending on any API. The Haiku KPI flags and PDL attributes
from the original run are preserved untouched (re-running them would cost money;
they read the claims directly, not the broken first_employer).

years_to_md is re-gated on the stored reached_md flag so the per-person row stays
internally consistent.

Usage:  python backfill_career_fields.py [--db data/titans.db] [--dry-run]
"""
from __future__ import annotations

import argparse
import dataclasses
import sqlite3

from career_analysis import (
    first_post_grad_employer,
    num_employers,
    years_to_md,
)
from enrichment_store import ClaimRow
from kpi_classify import deterministic_flags
from grad_year import derive_grad_year
from person_insights_store import (
    all_person_insights,
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


def backfill(db_path: str, dry_run: bool) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_person_insights_schema(conn)

    insights = all_person_insights(conn)
    changed = 0
    for ins in insights:
        claims = _claims_for(conn, ins.person_id)
        school, titan_class = _person_meta(conn, ins.person_id)
        edu_texts = [
            f"{c.value} {c.quote}".strip()
            for c in claims
            if c.claim_type == "education"
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

        if (
            new_first == ins.first_employer
            and new_nemp == ins.num_employers
            and new_ytm == ins.years_to_md
        ):
            continue

        changed += 1
        sff_note = (
            f"; first-firm {ins.still_first_firm}->{new_sff}"
            if new_sff != ins.still_first_firm else ""
        )
        print(
            f"  #{ins.person_id}: "
            f"first '{ins.first_employer or '?'}' -> '{new_first or '?'}'; "
            f"nemp {ins.num_employers} -> {new_nemp}; "
            f"ytm {ins.years_to_md} -> {new_ytm}{sff_note}"
        )
        if dry_run:
            continue
        upsert_person_insight(
            conn,
            dataclasses.replace(
                ins,
                first_employer=new_first,
                num_employers=new_nemp,
                years_to_md=new_ytm,
                still_first_firm=new_sff,
                started_sell_side=new_sss,
            ),
        )

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
