"""Experiment: Perplexity Agent (people_search + web_search) run through OUR noise
filters instead of the agent's self-verdict — the bake-off's "option (b)".

For each person:
  1. run_agent -> raw mentions (the agent's own is_this_person is IGNORED here),
  2. drop data-broker / aggregator domains (news_score.is_aggregator_domain),
  3. Haiku identity gate (news_verify.verify_hits) — keep only "yes",
  4. report: raw -> after-aggregator -> after-Haiku, LinkedIn URL found, kept items,
     and whether anything is NEW vs the claims already stored for that person.

Prints a per-person trace + a summary table + total API cost (read from the
Perplexity usage, not estimated). Read-only on the DB. Spends real money
(~$0.026/person agent + a cheap Haiku gate) — scoped to a name list.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from urllib.parse import urlparse

import httpx
from anthropic import Anthropic
from dotenv import load_dotenv

from news_score import is_aggregator_domain, normalize_domain
from news_verify import Candidate, verify_hits
from perplexity_agent import run_agent

CLASS3 = [
    "Madison Adams", "Lauren Bicknell", "Kimberly Carey", "Shaun Frederiksen",
    "Byron Geeslin", "Danny Pohlman", "Michael Rooney", "Komson Silapachai",
    "Laura Smith", "Ross Willmann",
]


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def _is_linkedin(url: str) -> bool:
    return "linkedin.com/in/" in url.lower()


def _person(conn: sqlite3.Connection, name: str):
    return conn.execute(
        "SELECT id, full_name, "
        "COALESCE(NULLIF(research_company,''), initial_company) AS company, city "
        "FROM people WHERE full_name = ? LIMIT 1",
        (name,),
    ).fetchone()


def _known_hosts(conn: sqlite3.Connection, pid: int) -> set[str]:
    rows = conn.execute(
        "SELECT source_url FROM claims WHERE person_id = ? AND source_url <> ''", (pid,)
    ).fetchall()
    return {_host(r["source_url"]) for r in rows if r["source_url"]}


def run(names: list[str], db_path: str) -> None:
    load_dotenv()
    pkey = os.environ["PERPLEXITY_API_KEY"]
    anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    total_cost = 0.0
    summary = []
    with httpx.Client(timeout=120.0) as http:
        for name in names:
            p = _person(conn, name)
            if p is None:
                print(f"\n=== {name} === (not in roster, skipped)")
                continue
            known = _known_hosts(conn, p["id"])
            res = run_agent(
                http, pkey, p["full_name"],
                employer=p["company"], city=p["city"],
            )
            total_cost += res.cost_usd
            raw = list(res.mentions)
            after_agg = [m for m in raw if not is_aggregator_domain(m.url)]
            cands = [
                Candidate(title=m.title, snippet=m.snippet, domain=normalize_domain(m.url))
                for m in after_agg
            ]
            verdicts = verify_hits(anthropic, p["full_name"], p["company"], p["city"], cands)
            kept = [m for m, v in zip(after_agg, verdicts) if v.is_match]

            linkedin = next((m.url for m in kept if _is_linkedin(m.url)), "")
            new_hosts = {_host(m.url) for m in kept} - known

            print(f"\n=== {name} | {p['company']} | {p['city']} ===")
            print(
                f"  raw {len(raw)} -> after-aggregator {len(after_agg)} "
                f"-> Haiku-kept {len(kept)}  | cost ${res.cost_usd:.4f}"
                f"{'  [error: ' + res.error + ']' if res.error else ''}"
            )
            print(f"  LinkedIn: {linkedin or '(none)'}")
            for m in kept:
                tag = "NEW" if _host(m.url) in new_hosts else "dup"
                print(f"    [{tag}] {_host(m.url):28} {m.title[:60]}")
            summary.append((name, len(raw), len(after_agg), len(kept),
                            bool(linkedin), len(new_hosts), res.cost_usd))

    conn.close()
    print("\n" + "=" * 78)
    print(f"{'name':22} {'raw':>4} {'agg':>4} {'kept':>4} {'LI':>3} {'new':>4} {'cost':>8}")
    for n, raw, agg, kept, li, new, cost in summary:
        print(f"{n:22} {raw:>4} {agg:>4} {kept:>4} {'Y' if li else '-':>3} {new:>4} ${cost:>6.4f}")
    n = len(summary) or 1
    print("-" * 78)
    print(f"TOTAL agent cost ${total_cost:.4f} for {len(summary)} people "
          f"(${total_cost / n:.4f}/person)")
    li_count = sum(1 for s in summary if s[4])
    kept_any = sum(1 for s in summary if s[3] > 0)
    print(f"LinkedIn URL found: {li_count}/{len(summary)} | "
          f"kept >=1 verified item: {kept_any}/{len(summary)}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/titans.db")
    ap.add_argument("--names", nargs="*", default=CLASS3)
    args = ap.parse_args()
    run(args.names, args.db)


if __name__ == "__main__":
    main()
