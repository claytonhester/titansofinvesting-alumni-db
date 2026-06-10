"""Firecrawl-free deep-research run for already-enriched alumni ("the 48").

Layers two OFF-Firecrawl sources onto people who already have a structured profile,
APPEND-ONLY — it never rewrites the reconciled career/education:

  * Perplexity Sonar press discovery (sonar_news) -> news_mention claims -> the
    strict Haiku news curator (news_curate) -> curated news feed. Closes the biggest
    gap: most of the 48 have no news yet.
  * PDL person-enrich (pdl_enrich + pdl_verify) used ONLY to fill a MISSING
    location / current-employer / current-title -> identity-gated structured fill.

Safety:
  * DRY-RUN by default: prints scope + a cost estimate, makes NO API calls and NO
    DB writes. Pass --apply to run for real.
  * --apply backs up the DB first, enforces a hard --max-usd cap (default 3.0), and
    prints running cost. --pilot N (default 3) / --name / --limit scope the run;
    --no-sonar / --no-pdl toggle sources.
  * Records the real spend to the cost log (cost_log), so this run is metered.
  * Never raises per person: a source failure degrades that person, the loop goes on.

    python research_run.py                       # dry run over all 48
    python research_run.py --apply --pilot 3      # real run, first 3 people
    python research_run.py --apply                # real run, all 48 (after pilot)
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

import httpx
from anthropic import Anthropic

import config  # noqa: F401 — import triggers load_dotenv() for API keys
from cost_log import (
    PDL_USD_PER_MATCH,
    PERPLEXITY_USD_PER_REQUEST,
    append_entry,
    build_entry,
    claude_usd,
)
from config import DB_PATH
from db import connect
from enrichment_store import ClaimRow, append_claims
from http_fetch import fetch_article
from news_curate import curate_news
from news_store import replace_curated_news
from pdl_enrich import enrich_pdl
from pdl_verify import verify_pdl_claims
from sonar_news import discover_press_sonar

_SONAR_FACETS = 3          # discover_press_sonar issues one call per facet
_SONAR_USD_PER_FACET = 0.006
_GAP_FILL_TYPES = ("location", "current_employer", "current_title")


def _load_people(conn, name: str | None) -> list[dict]:
    """The enriched cohort (anyone with >=1 claim) plus query anchors and per-field
    gap flags, so we only pay PDL for the people actually missing structured data."""
    sql = """
        SELECT p.id, p.full_name, p.initial_company AS company, p.city, p.school,
               MAX(CASE WHEN c.claim_type='current_employer' THEN c.value END) AS employer,
               MAX(CASE WHEN c.claim_type='location'         THEN 1 ELSE 0 END) AS has_location,
               MAX(CASE WHEN c.claim_type='current_employer' THEN 1 ELSE 0 END) AS has_employer,
               MAX(CASE WHEN c.claim_type='current_title'    THEN 1 ELSE 0 END) AS has_title
        FROM people p
        JOIN claims c ON c.person_id = p.id
    """
    if name:
        rows = conn.execute(sql + " WHERE p.full_name = ? GROUP BY p.id ORDER BY p.id", (name,)).fetchall()
    else:
        rows = conn.execute(sql + " GROUP BY p.id ORDER BY p.id").fetchall()
    return [dict(r) for r in rows]


def _news_inputs(conn, person_id: int) -> list[ClaimRow]:
    """A person's curatable items: news_mention claims + press-worthy public_links."""
    rows = conn.execute(
        """SELECT claim_type, value, source_url, quote, confidence, extraction_method
           FROM claims WHERE person_id = ? AND claim_type IN ('news_mention', 'public_links')""",
        (person_id,),
    ).fetchall()
    return [ClaimRow(r["claim_type"], r["value"], r["source_url"], r["quote"],
                     r["confidence"], r["extraction_method"]) for r in rows]


def _gaps(person: dict) -> list[str]:
    missing = []
    if not person["has_location"]:
        missing.append("location")
    if not person["has_employer"]:
        missing.append("current_employer")
    return missing


def _estimate_usd(people: list[dict], *, sonar: bool, pdl: bool) -> float:
    total = 0.0
    for p in people:
        if sonar:
            total += _SONAR_FACETS * _SONAR_USD_PER_FACET
        if pdl and _gaps(p):
            total += PDL_USD_PER_MATCH
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="run for real (default: dry run)")
    ap.add_argument("--name", help="single person by exact full name")
    ap.add_argument("--pilot", type=int, metavar="N", help="only the first N people")
    ap.add_argument("--limit", type=int, metavar="N", help="alias for --pilot")
    ap.add_argument("--no-sonar", action="store_true", help="skip Perplexity press discovery")
    ap.add_argument("--no-pdl", action="store_true", help="skip PDL gap-fill")
    ap.add_argument("--max-usd", type=float, default=3.0, help="hard cost cap (default 3.0)")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args()

    sonar_on = not args.no_sonar
    pdl_on = not args.no_pdl
    cap = max(args.pilot or 0, args.limit or 0) or None

    with connect(Path(args.db)) as conn:
        people = _load_people(conn, args.name)
    if cap:
        people = people[:cap]
    if not people:
        print("No matching enriched people.", file=sys.stderr)
        return 1

    est = _estimate_usd(people, sonar=sonar_on, pdl=pdl_on)
    gap_people = sum(1 for p in people if _gaps(p))
    print(f"Scope: {len(people)} people | sonar={sonar_on} pdl={pdl_on} "
          f"(PDL gap-fill candidates: {gap_people})")
    print(f"Estimated cost: ~${est:.2f}  (cap ${args.max_usd:.2f})")

    if not args.apply:
        print("\nDRY RUN — no API calls, no DB writes. Re-run with --apply to execute.")
        for p in people:
            g = _gaps(p)
            print(f"  [{p['id']}] {p['full_name']:<22} gaps: {', '.join(g) or 'none'}")
        return 0

    pplx_key = os.getenv("PERPLEXITY_API_KEY") if sonar_on else None
    pdl_key = os.getenv("PDL_API_KEY") if pdl_on else None
    anthro_key = os.getenv("ANTHROPIC_API_KEY")
    if sonar_on and not pplx_key:
        print("PERPLEXITY_API_KEY not set — Sonar disabled.")
    if pdl_on and not pdl_key:
        print("PDL_API_KEY not set — PDL disabled.")
    if not anthro_key:
        print("ANTHROPIC_API_KEY not set — cannot curate/verify. Aborting.", file=sys.stderr)
        return 1

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{args.db}.bak-pre-research-{stamp}"
    # Checkpoint the WAL into the main file FIRST, else copy2 captures an
    # inconsistent snapshot (the main .db without the pending -wal frames).
    with connect(Path(args.db)) as _c:
        _c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(args.db, backup)
    print(f"Backed up -> {backup}\n")

    anthro = Anthropic(api_key=anthro_key)
    sonar_usd = pdl_usd = 0.0
    sonar_reqs = pdl_matches = 0
    haiku_in = haiku_out = 0
    n_news = n_fills = 0

    with httpx.Client(timeout=90.0) as http, connect(Path(args.db)) as conn:
        for p in people:
            pid, name = p["id"], p["full_name"]
            emp = p["employer"] or p["company"] or ""
            city = p["city"] or ""
            running = sonar_usd + pdl_usd + claude_usd(haiku_in, haiku_out, 0, 0)
            if running >= args.max_usd:
                print(f"\nCost cap ${args.max_usd:.2f} reached (${running:.2f}). Stopping.")
                break
            print(f"[{pid}] {name}")

            # 1. Perplexity Sonar press discovery -> news_mention claims.
            if pplx_key:
                res = discover_press_sonar(http, name, emp, city, perplexity_key=pplx_key)
                sonar_usd += res.cost_usd
                sonar_reqs += res.requests
                if res.claim_rows:
                    conn.execute(
                        "DELETE FROM claims WHERE person_id=? AND claim_type='news_mention' "
                        "AND extraction_method='sonar-pro'", (pid,))
                    append_claims(conn, pid, list(res.claim_rows))
                print(f"  sonar: {res.kept} kept / {res.found} found  (${res.cost_usd:.4f})")

            # 2. Curate the pooled mentions into the shown feed. Article-verified via
            # a free HTTP fetch (no Firecrawl): the curator reads each page around the
            # person's name and DROPS items where they aren't the subject — the gate
            # that catches name-dropped misattributions (Ross / Forty-Under-Forty). An
            # unfetchable page -> "" -> dropped (precision over recall).
            mentions = _news_inputs(conn, pid)
            curated, hin, hout = curate_news(
                anthro, name, emp, mentions, fetch_article=fetch_article
            )
            haiku_in += hin
            haiku_out += hout
            replace_curated_news(conn, pid, curated)
            n_news += len(curated)
            print(f"  curated news: {len(curated)}")

            # 3. PDL gap-fill — only when a structured field is actually missing.
            gaps = _gaps(p)
            if pdl_key and gaps:
                pres = enrich_pdl(http, pdl_key, name, emp, city,
                                  school=p["school"] or "", cost_usd_per_match=PDL_USD_PER_MATCH)
                pdl_usd += pres.cost_usd
                pdl_matches += 1 if pres.matched else 0
                if pres.claim_rows:
                    kept, vin, vout = verify_pdl_claims(anthro, name, emp, city, list(pres.claim_rows))
                    haiku_in += vin
                    haiku_out += vout
                    want = set(gaps) | ({"current_title"} if not p["has_title"] else set())
                    fill = [c for c in kept if c.claim_type in want and c.claim_type in _GAP_FILL_TYPES]
                    if fill:
                        append_claims(conn, pid, fill)
                        n_fills += len(fill)
                    print(f"  pdl: match={pres.matched} filled={len(fill)}  (${pres.cost_usd:.4f})")
                else:
                    print(f"  pdl: no match  (${pres.cost_usd:.4f})")
            conn.commit()

    total = sonar_usd + pdl_usd + claude_usd(haiku_in, haiku_out, 0, 0)
    print("\n" + "=" * 56)
    print(f"news rows: {n_news} | gap fills: {n_fills}")
    print(f"sonar: {sonar_reqs} reqs ${sonar_usd:.4f} | pdl: {pdl_matches} matches "
          f"${pdl_usd:.4f} | haiku {haiku_in}+{haiku_out} tok ${claude_usd(haiku_in, haiku_out, 0, 0):.4f}")
    print(f"TOTAL: ${total:.4f}")

    entry = build_entry(
        label="research_run", people=len(people),
        haiku_in=haiku_in, haiku_out=haiku_out,
        pdl_matches=pdl_matches, sonar_requests=sonar_reqs, sonar_usd=sonar_usd,
    )
    append_entry(entry)
    print("Logged to cost log. Next: `npm run sync-db` from web/, then restart the dev server.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
