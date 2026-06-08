"""Layer PDL + Firecrawl press-news + verified public mentions onto already-
enriched people.

Reads career data from the DB, runs only the optional sources, and APPENDS the
new claims without touching existing career/education/bio rows. Use this to
backfill deeper data on people already enriched without re-running the expensive
Firecrawl discovery step — e.g. to add the Perplexity+Haiku verified-mention pass
to profiles enriched before it existed.

    python enrich_news_only.py                   # all 'done' people
    python enrich_news_only.py --name "Jason Kaspar"
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx
from anthropic import Anthropic
from firecrawl import Firecrawl

from config import DB_PATH, require_key
from db import connect
from discovery import discover_news
from enrichment_store import ClaimRow, replace_claims
from firecrawl.v2.utils.error_handler import PaymentRequiredError
from mention_discovery import discover_mentions
from news_enrich import extract_news_mentions
from normalize import digest_claims
from pdl_enrich import enrich_pdl
from pdl_verify import verify_pdl_claims
from reconcile import reconcile_claims
from cost_log import PDL_USD_PER_MATCH


def _load_done_people(conn, name: str | None) -> list[dict]:
    """Load enriched people plus their verified employer/title from claims so
    the news pass uses profile-aware queries instead of the raw directory string."""
    base_sql = """
        SELECT p.id, p.full_name, p.initial_company AS company, p.city,
               MAX(CASE WHEN c.claim_type='current_employer' THEN c.value END) AS verified_employer,
               MAX(CASE WHEN c.claim_type='current_title'    THEN c.value END) AS verified_title
        FROM people p
        JOIN batch_status b ON b.person_id = p.id AND b.phase = 'structuring' AND b.status = 'done'
        LEFT JOIN claims c ON c.person_id = p.id
          AND c.claim_type IN ('current_employer', 'current_title')
          AND c.extraction_method != 'gnews'
          AND c.extraction_method != 'firecrawl_news'
    """
    if name:
        rows = conn.execute(
            base_sql + " WHERE p.full_name = ? GROUP BY p.id", (name,)
        ).fetchall()
    else:
        rows = conn.execute(
            base_sql + " GROUP BY p.id ORDER BY p.id"
        ).fetchall()
    return [dict(r) for r in rows]


def run(name: str | None) -> int:
    firecrawl = Firecrawl(api_key=require_key("FIRECRAWL_API_KEY"))
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
    pdl_key = os.getenv("PDL_API_KEY")
    perplexity_key = os.getenv("PERPLEXITY_API_KEY")

    with connect(DB_PATH) as conn:
        people = _load_done_people(conn, name)
        if not people:
            print("No enriched people found.", file=sys.stderr)
            return 1

        print(f"Layering news+PDL onto {len(people)} already-enriched people...\n")

        with httpx.Client(timeout=30.0) as http:
            for p in people:
                pid = p["id"]
                full_name = p["full_name"]
                company = p["company"] or ""
                city = p["city"] or ""
                new_claims = []

                print(f"=== {full_name} | {company} ===")

                # ── PDL ──────────────────────────────────────────────────────
                if pdl_key:
                    pdl = enrich_pdl(
                        http, pdl_key, full_name, company, city,
                        cost_usd_per_match=PDL_USD_PER_MATCH,
                    )
                    if pdl and pdl.claim_rows:
                        kept_pdl, _, _ = verify_pdl_claims(
                            anthropic, full_name,
                            p.get("verified_employer") or company, city,
                            list(pdl.claim_rows),
                        )
                        new_claims.extend(kept_pdl)
                        gated = len(pdl.claim_rows) - len(kept_pdl)
                        print(f"  PDL: {len(kept_pdl)} claims "
                              f"(matched={pdl.matched}, ${pdl.cost_usd:.4f}"
                              f"{f', -{gated} gated' if gated else ''})")
                    else:
                        print("  PDL: no match")
                else:
                    print("  PDL: key not set — skipped")

                # ── Firecrawl press news ─────────────────────────────────────
                verified_employer = p.get("verified_employer") or ""
                verified_title = p.get("verified_title") or ""
                if verified_employer:
                    print(f"  Using verified profile: {verified_title or '(no title)'} @ {verified_employer}")
                try:
                    news_disc = discover_news(
                        firecrawl, full_name, company,
                        verified_employer=verified_employer,
                        verified_title=verified_title,
                    )
                    fc_news = extract_news_mentions(
                        anthropic, full_name, verified_employer or company, news_disc
                    )
                    if fc_news.claim_rows:
                        new_claims.extend(fc_news.claim_rows)
                        print(f"  Press news: {len(fc_news.claim_rows)} verified articles "
                              f"({news_disc.credits_spent} Firecrawl credits)")
                    else:
                        print(f"  Press news: no results "
                              f"({news_disc.credits_spent} credits spent)")
                except PaymentRequiredError:
                    print("  Press news: skipped — no Firecrawl credits "
                          "(top up at firecrawl.dev)")

                # ── Perplexity + Haiku verified mentions ─────────────────────
                if perplexity_key:
                    mentions = discover_mentions(
                        http, anthropic, full_name,
                        verified_employer or company, city,
                        perplexity_key=perplexity_key,
                    )
                    if mentions.claim_rows:
                        new_claims.extend(mentions.claim_rows)
                        print(f"  Mentions: {mentions.verified} verified of "
                              f"{mentions.after_filter} (found {mentions.found})")
                    else:
                        print(f"  Mentions: 0 verified "
                              f"(found {mentions.found}, kept {mentions.after_filter})")
                else:
                    print("  Mentions: PERPLEXITY_API_KEY not set — skipped")

                # ── Persist ──────────────────────────────────────────────────
                # Load ALL existing claims, merge with new, normalize case,
                # deduplicate, then replace the full set. No leftover stale
                # rows, no duplicates, every re-run produces a clean profile.
                existing_rows = conn.execute(
                    "SELECT claim_type, value, source_url, quote, confidence, "
                    "extraction_method FROM claims WHERE person_id = ?",
                    (pid,),
                ).fetchall()
                existing = [
                    ClaimRow(
                        claim_type=r["claim_type"],
                        value=r["value"],
                        source_url=r["source_url"],
                        quote=r["quote"] or "",
                        confidence=r["confidence"],
                        extraction_method=r["extraction_method"],
                    )
                    for r in existing_rows
                ]
                # Reconcile the full set (existing + new) so multi-source résumé
                # facts collapse semantically before the deterministic digest.
                reconciled, _, _ = reconcile_claims(anthropic, full_name, existing + new_claims)
                merged = digest_claims(reconciled)
                replace_claims(conn, pid, merged)
                conn.commit()
                delta = len(merged) - len(existing)
                print(f"  → {len(merged)} total claims "
                      f"({delta:+d} vs before, after dedup + normalize)")

        print(f"\nDone. Run the web app to see updated profiles.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="enrich-news-only", description=__doc__)
    p.add_argument("--name", default=None, help="One specific person by full name")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(name=args.name)


if __name__ == "__main__":
    sys.exit(main())
