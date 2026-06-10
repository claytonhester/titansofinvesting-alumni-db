"""Probe: run ONLY the new news-research method on already-enriched people, and
meter the exact cost. READ-ONLY (no DB writes), Firecrawl-free.

Isolates the news path from the rest of enrichment so we can see, on people we
already have data for, what the de-biased + career-aware Sonar discovery surfaces
and what the strict Jina-verified curator keeps — plus the precise spend:

    stored data (employer, title, PDL industry, past firms)
        -> discover_press_sonar(...)  [Perplexity Sonar]   -> news_mention claims
        -> curate_news(..., fetch_article=Jina, career=...) [Haiku + free Jina]
        -> the curated feed that WOULD show

Cost = Perplexity Sonar (authoritative usage.cost) + Haiku curate/verify tokens.

    python news_research_probe.py --limit 8
    python news_research_probe.py --name "Ross Willmann"
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx
from anthropic import Anthropic

from config import DB_PATH, require_key
from cost_log import HAIKU_USD_PER_MTOK_IN, HAIKU_USD_PER_MTOK_OUT
from db import connect, init_schema
from enrichment_store import init_enrichment_schema
from http_fetch import fetch_article as fetch_article_jina
from news_curate import curate_news
from person_insights_store import init_person_insights_schema
from person_company_store import init_person_company_schema
from sonar_news import discover_press_sonar


def _stored_inputs(conn, person_id: int) -> dict:
    """Pull the news-method inputs we already persisted for this person."""
    def _claim(ct: str) -> str:
        row = conn.execute(
            "SELECT value FROM claims WHERE person_id=? AND claim_type=? "
            "ORDER BY confidence DESC LIMIT 1",
            (person_id, ct),
        ).fetchone()
        return row["value"] if row else ""

    industry = ""
    row = conn.execute(
        "SELECT current_industry FROM person_insights WHERE person_id=?", (person_id,)
    ).fetchone()
    if row and row["current_industry"]:
        industry = row["current_industry"]

    past = [
        r["company_name"]
        for r in conn.execute(
            "SELECT company_name FROM person_company "
            "WHERE person_id=? AND is_current=0 AND company_name<>''",
            (person_id,),
        ).fetchall()
    ]
    # de-dupe, preserve order
    past = list(dict.fromkeys(past))
    return {
        "employer": _claim("current_employer"),
        "title": _claim("current_title"),
        "industry": industry,
        "past": tuple(past),
    }


def _load_people(conn, limit: int, name: str | None) -> list:
    if name:
        sql = ("SELECT p.id,p.full_name,p.initial_company,p.city FROM people p "
               "WHERE p.full_name=?")
        return conn.execute(sql, (name,)).fetchall()
    sql = ("SELECT p.id,p.full_name,p.initial_company,p.city,COUNT(cl.id) n "
           "FROM people p JOIN claims cl ON cl.person_id=p.id "
           "GROUP BY p.id HAVING n>5 ORDER BY n DESC LIMIT ?")
    return conn.execute(sql, (limit,)).fetchall()


def run(limit: int, name: str | None) -> int:
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
    perplexity_key = os.getenv("PERPLEXITY_API_KEY")
    if not perplexity_key:
        print("PERPLEXITY_API_KEY unset — the news method needs it.", file=sys.stderr)
        return 1

    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_enrichment_schema(conn)
        init_person_insights_schema(conn)
        init_person_company_schema(conn)
        people = _load_people(conn, limit, name)
        inputs = {r["id"]: _stored_inputs(conn, r["id"]) for r in people}

    if not people:
        print("No enriched people matched.", file=sys.stderr)
        return 1

    sonar_cost = haiku_in = haiku_out = sonar_reqs = 0.0
    raw_total = shown_total = 0
    print(f"News-research probe (READ-ONLY, no Firecrawl) on {len(people)} people\n")

    with httpx.Client(timeout=30.0) as http:
        for r in people:
            inp = inputs[r["id"]]
            employer = inp["employer"] or r["initial_company"]
            sonar = discover_press_sonar(
                http, r["full_name"], employer, r["city"],
                perplexity_key=perplexity_key,
                role=inp["title"], industry=inp["industry"],
                past_companies=inp["past"],
            )
            sonar_cost += sonar.cost_usd
            sonar_reqs += sonar.requests
            raw_total += sonar.found

            curated, tin, tout = curate_news(
                anthropic, r["full_name"], employer, list(sonar.claim_rows),
                fetch_article=fetch_article_jina, career=inp["past"],
            )
            haiku_in += tin
            haiku_out += tout
            shown_total += len(curated)

            cost = sonar.cost_usd + (tin / 1e6) * HAIKU_USD_PER_MTOK_IN + (tout / 1e6) * HAIKU_USD_PER_MTOK_OUT
            print(f"=== {r['full_name']} | {employer} ===")
            ctx = f"role={inp['title'] or '?'}; industry={inp['industry'] or '?'}"
            if inp["past"]:
                ctx += f"; past={', '.join(inp['past'][:3])}"
            print(f"  inputs: {ctx}")
            print(f"  sonar: {sonar.requests} calls, {sonar.found} raw items -> {len(curated)} shown  (${cost:.4f})")
            for n in curated:
                print(f"    ✓ [{n.category}] {n.headline}")
                if n.summary:
                    print(f"        {n.summary}")
            if not curated:
                print("    (nothing cleared the curator)")
            print()

    haiku_cost = (haiku_in / 1e6) * HAIKU_USD_PER_MTOK_IN + (haiku_out / 1e6) * HAIKU_USD_PER_MTOK_OUT
    total = sonar_cost + haiku_cost
    n = len(people)
    print("─" * 60)
    print(f"  people:            {n}")
    print(f"  raw items found:   {raw_total}  ->  shown: {shown_total}")
    print(f"  Sonar calls:       {int(sonar_reqs)}  (Perplexity)")
    print(f"  Sonar cost:        ${sonar_cost:.4f}")
    print(f"  Haiku curate cost: ${haiku_cost:.4f}  ({haiku_in:,} in / {haiku_out:,} out tok)")
    print(f"  TOTAL:             ${total:.4f}   (${total / n:.4f}/person)")
    print(f"  Projected x1,008:  ${total / n * 1008:.2f}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="news-research-probe", description=__doc__)
    p.add_argument("--limit", type=int, default=8, help="How many enriched people to probe")
    p.add_argument("--name", default=None, help="Probe one specific person")
    args = p.parse_args(argv)
    return run(limit=args.limit, name=args.name)


if __name__ == "__main__":
    sys.exit(main())
