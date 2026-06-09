"""Apply the deterministic profile cleanup + re-curate news on ALREADY-COLLECTED
claims — no re-collection, so it costs nothing on Firecrawl/PDL (only a small
Haiku news-curation call per person).

Run AFTER deploying profile_cleanup + the news_curate public_links fix to refresh
the existing demo cohort:

    python backfill_cleanup.py              # clean claims + re-curate news
    python backfill_cleanup.py --no-llm     # clean claims only (free, no Haiku)

Follow with backfill_career_fields.py to re-derive first_employer/num_employers
from the cleaned career history, then phase3_insights to rebuild the snapshot.
"""
from __future__ import annotations

import argparse
import os
import sqlite3

from anthropic import Anthropic
from dotenv import load_dotenv

from enrichment_store import ClaimRow, init_enrichment_schema, replace_claims
from news_curate import curate_news
from news_store import init_news_schema, replace_curated_news
from profile_cleanup import clean_profile


def _claims_for(conn: sqlite3.Connection, person_id: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    return [
        ClaimRow(
            r["claim_type"], r["value"], r["source_url"],
            r["quote"], r["confidence"], r["extraction_method"],
        )
        for r in rows
    ]


def _employer(claims: list[ClaimRow]) -> str:
    return next((c.value for c in claims if c.claim_type == "current_employer"), "")


def backfill(db_path: str, use_llm: bool) -> None:
    load_dotenv()
    client = None
    if use_llm:
        key = os.getenv("ANTHROPIC_API_KEY")
        client = Anthropic(api_key=key) if key else None

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_enrichment_schema(conn)
    init_news_schema(conn)

    people = conn.execute(
        "SELECT DISTINCT person_id FROM claims ORDER BY person_id"
    ).fetchall()

    cleaned_total = news_total = 0
    for (pid,) in people:
        claims = _claims_for(conn, pid)
        cleaned = clean_profile(claims)
        dropped = len(claims) - len(cleaned)
        if dropped:
            replace_claims(conn, pid, cleaned)
            cleaned_total += 1

        name_row = conn.execute(
            "SELECT full_name FROM people WHERE id = ?", (pid,)
        ).fetchone()
        name = name_row["full_name"] if name_row else ""
        curated, _, _ = curate_news(client, name, _employer(cleaned), cleaned)
        replace_curated_news(conn, pid, curated)
        if curated:
            news_total += 1
            print(f"  #{pid} {name}: -{dropped} junk claims, {len(curated)} news items")
        elif dropped:
            print(f"  #{pid} {name}: -{dropped} junk claims, no news")

    conn.commit()
    conn.close()
    print(
        f"\nCleaned {cleaned_total}/{len(people)} profiles; "
        f"curated news for {news_total} people."
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/titans.db")
    ap.add_argument("--no-llm", action="store_true", help="skip Haiku news curation")
    args = ap.parse_args()
    backfill(args.db, use_llm=not args.no_llm)


if __name__ == "__main__":
    main()
