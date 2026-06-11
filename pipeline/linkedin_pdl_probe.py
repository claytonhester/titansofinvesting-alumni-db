"""Read-only head-to-head: what an orgbase-style LinkedIn read returns vs PDL.

For each person id, seed the (now tolerant, uncapped) LinkedIn agent with the
profile URL already on file and compare its yield to what PDL gave. Persists
NOTHING — pure measurement so we can finally settle the Firecrawl-vs-PDL plan.

    python linkedin_pdl_probe.py 2 198 277 356 593 672 790
"""
from __future__ import annotations

import os
import sys

import config  # noqa: F401 — triggers .env load
from db import connect
from config import DB_PATH
from firecrawl import Firecrawl
from linkedin_firecrawl import fetch_linkedin
from phase2_enrich import _candidate_linkedin_url
from scorecard import _load_claims

PROBE_MAX_CREDITS = 1000  # high enough that the agent never refuses on a single read


def _career_count(claims, *, source_substr=None):
    n = 0
    for c in claims:
        if c.claim_type != "career_history":
            continue
        if source_substr and source_substr not in c.extraction_method:
            continue
        n += 1
    return n


def main(ids: list[int]) -> int:
    client = Firecrawl(api_key=os.getenv("FIRECRAWL_API_KEY"))
    rows = []
    with connect(DB_PATH) as conn:
        for pid in ids:
            p = conn.execute(
                "SELECT full_name, initial_company, city FROM people WHERE id=?",
                (pid,),
            ).fetchone()
            claims = list(_load_claims(conn, pid))
            url = _candidate_linkedin_url(claims)
            pdl_career = _career_count(claims, source_substr="pdl")
            pdl_emp = next((c.value for c in claims
                            if c.claim_type == "current_employer"), "")
            if not url:
                rows.append((pid, p["full_name"], "(no url)", 0, 0, pdl_career, pdl_emp, ""))
                continue
            res = fetch_linkedin(client, p["full_name"], employer=p["initial_company"] or "",
                                 city=p["city"] or "", profile_url=url,
                                 max_credits=PROBE_MAX_CREDITS)
            li_career = sum(1 for c in res.claim_rows if c.claim_type == "career_history")
            li_emp = next((c.value for c in res.claim_rows
                           if c.claim_type == "current_employer"), "")
            rows.append((pid, p["full_name"], "found" if res.found else "NOT FOUND",
                         res.credits_used, li_career, pdl_career, pdl_emp, li_emp))

    print(f"\n{'id':>4} {'name':<22} {'LI read':>9} {'cr':>4} {'LI roles':>8} "
          f"{'PDL roles':>9}  current (PDL | LinkedIn)")
    tot_cr = tot_li = tot_pdl = found = 0
    for pid, nm, status, cr, li, pdl, pemp, lemp in rows:
        print(f"{pid:>4} {nm:<22} {status:>9} {cr:>4} {li:>8} {pdl:>9}  "
              f"{(pemp or '-')[:24]} | {(lemp or '-')[:24]}")
        tot_cr += cr
        tot_li += li
        tot_pdl += pdl
        found += 1 if status == "found" else 0
    print(f"\nLinkedIn found {found}/{len(rows)} | total credits {tot_cr} "
          f"(~{tot_cr // max(found,1)}/read) | roles: LinkedIn {tot_li} vs PDL {tot_pdl}")
    return 0


if __name__ == "__main__":
    args = [int(x) for x in sys.argv[1:]] or [2, 198, 277, 356, 593, 672, 790]
    raise SystemExit(main(args))
