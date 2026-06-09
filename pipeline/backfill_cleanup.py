"""Apply the deterministic profile cleanup + re-curate news on ALREADY-COLLECTED
claims — no re-collection, so it costs nothing on Firecrawl/PDL (only a small
Haiku news-curation call per person).

Run AFTER deploying profile_cleanup + the news_curate public_links fix to refresh
the existing demo cohort:

    python backfill_cleanup.py              # clean claims + re-curate news
    python backfill_cleanup.py --no-llm     # clean claims only (free, no Haiku)
    python backfill_cleanup.py --sonar      # ALSO add Perplexity Sonar press first

With --sonar, each person first gets the Sonar press-discovery pass (cited,
person-specific press, ~$0.008/person on Perplexity — OFF the Firecrawl budget);
those news_mention claims are merged in before curation, so the new strict
subject_depth curator can surface them. Without the flag the script stays free
(Firecrawl/PDL untouched).

Follow with backfill_career_fields.py to re-derive first_employer/num_employers
from the cleaned career history, then phase3_insights to rebuild the snapshot.
"""
from __future__ import annotations

import argparse
import os
import sqlite3

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from article_context import make_firecrawl_fetcher
from enrichment_store import ClaimRow, init_enrichment_schema, replace_claims
from news_curate import curate_news
from news_store import init_news_schema, replace_curated_news
from normalize import digest_claims
from profile_cleanup import clean_profile
from sonar_news import discover_press_sonar


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


def backfill(db_path: str, use_llm: bool, use_sonar: bool = False) -> None:
    load_dotenv()
    client = None
    if use_llm:
        key = os.getenv("ANTHROPIC_API_KEY")
        client = Anthropic(api_key=key) if key else None

    perplexity_key = os.getenv("PERPLEXITY_API_KEY") if use_sonar else None
    if use_sonar and not perplexity_key:
        print("  --sonar requested but PERPLEXITY_API_KEY not set — skipping Sonar")

    # Article-verification fetcher: lets re-curation confirm a would-be-shown item
    # against the real article (drops name-dropped-in-someone-else's-entry items,
    # fixes the exact achievement). Only the feed candidates are scraped, so the
    # Firecrawl cost is bounded to the (tiny) feed size.
    fetch_article = None
    if use_llm:
        fc_key = os.getenv("FIRECRAWL_API_KEY")
        if fc_key:
            from firecrawl import Firecrawl

            fetch_article = make_firecrawl_fetcher(Firecrawl(api_key=fc_key))
        else:
            print("  FIRECRAWL_API_KEY not set — news curation will skip article verification")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_enrichment_schema(conn)
    init_news_schema(conn)

    people = conn.execute(
        "SELECT DISTINCT person_id FROM claims ORDER BY person_id"
    ).fetchall()

    cleaned_total = news_total = 0
    sonar_added = 0
    sonar_cost = 0.0
    http = httpx.Client(timeout=90.0) if perplexity_key else None
    try:
        for (pid,) in people:
            claims = _claims_for(conn, pid)
            cleaned = clean_profile(claims)
            dropped = len(claims) - len(cleaned)  # junk removed by cleanup

            row = conn.execute(
                "SELECT full_name, city FROM people WHERE id = ?", (pid,)
            ).fetchone()
            name = row["full_name"] if row else ""
            city = (row["city"] if row else "") or ""
            employer = _employer(cleaned)

            # Optional Sonar press pass: add cited, person-specific press as
            # news_mention claims, then dedupe the merged set so a re-run stays clean.
            sonar_note = ""
            if http is not None and name:
                sonar = discover_press_sonar(
                    http, name, employer, city, perplexity_key=perplexity_key,
                )
                sonar_cost += sonar.cost_usd
                if sonar.claim_rows:
                    cleaned = digest_claims(cleaned + list(sonar.claim_rows))
                    sonar_added += 1
                    sonar_note = f", +{sonar.kept} sonar press"

            # Persist whenever the stored set changed (cleanup dropped rows OR Sonar
            # added rows). Compare against the original claim set.
            if cleaned != claims:
                replace_claims(conn, pid, cleaned)
                if dropped > 0:
                    cleaned_total += 1

            curated, _, _ = curate_news(
                client, name, employer, cleaned, fetch_article=fetch_article
            )
            replace_curated_news(conn, pid, curated)
            if curated:
                news_total += 1
            if curated or dropped or sonar_note:
                print(f"  #{pid} {name}: -{dropped} junk claims, "
                      f"{len(curated)} news items{sonar_note}")
    finally:
        if http is not None:
            http.close()

    conn.commit()
    conn.close()
    msg = (
        f"\nCleaned {cleaned_total}/{len(people)} profiles; "
        f"curated news for {news_total} people."
    )
    if use_sonar and perplexity_key:
        msg += f" Sonar press added for {sonar_added} people (${sonar_cost:.4f})."
    print(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/titans.db")
    ap.add_argument("--no-llm", action="store_true", help="skip Haiku news curation")
    ap.add_argument("--sonar", action="store_true",
                    help="also run the Perplexity Sonar press pass (spends ~$0.008/person)")
    args = ap.parse_args()
    backfill(args.db, use_llm=not args.no_llm, use_sonar=args.sonar)


if __name__ == "__main__":
    main()
