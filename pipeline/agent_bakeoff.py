"""Head-to-head: current Search+Haiku mention discovery vs the Perplexity Agent
API (with people_search + web_search).

Answers one question with REAL numbers, not estimates: if we switched the
verified-mention pass from the flat-rate /search endpoint to the token-billed
/v1/agent endpoint with people_search and friends, what changes in (a) cost and
(b) results?

Both paths run on the SAME sample people so the comparison is apples-to-apples:

  Path A (current production)  Perplexity /search  ->  drop aggregators
                               ->  Claude Haiku identity verify
      cost = $0.005 (flat search) + measured Haiku tokens

  Path B (agent)               Perplexity /v1/agent (people_search + web_search),
                               model reasons + identity-judges itself
      cost = usage.cost.total_cost  (EXACT, straight from the API response)

Costs in Path A are real too: the Haiku call's token usage is captured from the
live API response, not guessed.

    python agent_bakeoff.py --limit 3
    python agent_bakeoff.py --limit 3 --model openai/gpt-5-mini --tools people_search,web_search
"""
from __future__ import annotations

import argparse
import os
import sys

import httpx
from anthropic import Anthropic

from config import DB_PATH
from cost_log import HAIKU_USD_PER_MTOK_IN, HAIKU_USD_PER_MTOK_OUT
from db import connect
from news_score import is_aggregator_domain, normalize_domain
from news_verify import _SYSTEM, _build_user, _parse_verdicts, Candidate
from perplexity_agent import run_agent
from perplexity_enrich import fetch_perplexity
from structuring import HAIKU_MODEL

# Documented flat rate for the /search endpoint: $5 per 1,000 requests.
SEARCH_USD_PER_REQUEST = 5.0 / 1_000


def _load_sample(conn, limit: int, name: str | None) -> list[dict]:
    """Prefer already-enriched people (we know their verified employer/city, so
    both paths get the same high-quality query)."""
    base = """
        SELECT p.id, p.full_name, p.initial_company AS company, p.city,
               MAX(CASE WHEN c.claim_type='current_employer' THEN c.value END) AS verified_employer
        FROM people p
        JOIN batch_status b ON b.person_id = p.id AND b.phase='structuring' AND b.status='done'
        LEFT JOIN claims c ON c.person_id = p.id AND c.claim_type='current_employer'
          AND c.extraction_method NOT IN ('gnews','firecrawl_news')
    """
    if name:
        rows = conn.execute(base + " WHERE p.full_name=? GROUP BY p.id", (name,)).fetchall()
    else:
        rows = conn.execute(base + " GROUP BY p.id ORDER BY p.id LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def _haiku_cost(tok_in: int, tok_out: int) -> float:
    return tok_in / 1_000_000 * HAIKU_USD_PER_MTOK_IN + tok_out / 1_000_000 * HAIKU_USD_PER_MTOK_OUT


def _run_search_path(http, anthropic, person: dict) -> dict:
    """Path A: /search + aggregator drop + Haiku verify, capturing real Haiku
    token usage so the cost is measured, not estimated."""
    name = person["full_name"]
    employer = person.get("verified_employer") or person.get("company") or ""
    city = person.get("city") or ""

    results = fetch_perplexity(http, os.environ["PERPLEXITY_API_KEY"], name,
                              employer=employer, max_results=6)
    kept = [r for r in results if not is_aggregator_domain(r.url)]

    haiku_in = haiku_out = 0
    verified = 0
    confirmed_titles: list[str] = []
    if kept:
        candidates = [Candidate(r.title, r.snippet, normalize_domain(r.url)) for r in kept]
        try:
            resp = anthropic.messages.create(
                model=HAIKU_MODEL,
                max_tokens=1024,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": _build_user(name, employer, city, candidates)}],
            )
            haiku_in = resp.usage.input_tokens
            haiku_out = resp.usage.output_tokens
            text = "".join(b.text for b in resp.content if b.type == "text")
            verdicts = _parse_verdicts(text, len(candidates))
        except Exception as exc:  # pragma: no cover - network
            return {"error": str(exc), "found": len(results), "verified": 0, "cost": 0.0,
                    "titles": []}
        for v, r in zip(verdicts, kept):
            if v.is_match:
                verified += 1
                confirmed_titles.append(f"{r.title}  ({normalize_domain(r.url)})")

    cost = SEARCH_USD_PER_REQUEST + _haiku_cost(haiku_in, haiku_out)
    return {
        "found": len(results),
        "after_filter": len(kept),
        "verified": verified,
        "haiku_in": haiku_in,
        "haiku_out": haiku_out,
        "cost": cost,
        "titles": confirmed_titles,
    }


def _run_agent_path(http, person: dict, model: str, tools: tuple[str, ...]) -> dict:
    name = person["full_name"]
    employer = person.get("verified_employer") or person.get("company") or ""
    city = person.get("city") or ""
    res = run_agent(http, os.environ["PERPLEXITY_API_KEY"], name,
                    employer=employer, city=city, model=model, tools=tools)
    confirmed = res.confirmed
    return {
        "error": res.error,
        "found": len(res.mentions),
        "verified": len(confirmed),
        "input_tokens": res.input_tokens,
        "output_tokens": res.output_tokens,
        "tool_calls": res.tool_calls,
        "cost": res.cost_usd,
        "titles": [f"{m.title}  ({normalize_domain(m.url)})" for m in confirmed],
    }


def run(limit: int, name: str | None, model: str, tools: tuple[str, ...]) -> int:
    if not os.getenv("PERPLEXITY_API_KEY"):
        print("PERPLEXITY_API_KEY not set.", file=sys.stderr)
        return 1
    anthropic = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    with connect(DB_PATH) as conn:
        people = _load_sample(conn, limit, name)
    if not people:
        print("No enriched people found to sample.", file=sys.stderr)
        return 1

    print(f"Bake-off on {len(people)} people  |  agent model: {model}  "
          f"|  agent tools: {', '.join(tools)}\n")

    a_cost = b_cost = 0.0
    a_verified = b_verified = 0
    with httpx.Client(timeout=120.0) as http:
        for p in people:
            print(f"=== {p['full_name']} | {p.get('verified_employer') or p.get('company')} ===")
            a = _run_search_path(http, anthropic, p)
            b = _run_agent_path(http, p, model, tools)

            a_cost += a["cost"]; b_cost += b["cost"]
            a_verified += a["verified"]; b_verified += b["verified"]

            print(f"  A search+haiku : {a['verified']:>2} verified / {a.get('found',0)} found  "
                  f"| ${a['cost']:.5f}  (haiku {a.get('haiku_in',0)}/{a.get('haiku_out',0)} tok)")
            for t in a["titles"]:
                print(f"        - {t}")
            tc = ", ".join(f"{k}:{v}" for k, v in (b.get("tool_calls") or {}).items()) or "none"
            err = f"  ERROR: {b['error']}" if b.get("error") else ""
            print(f"  B agent        : {b['verified']:>2} verified / {b.get('found',0)} found  "
                  f"| ${b['cost']:.5f}  (tok {b.get('input_tokens',0)}/{b.get('output_tokens',0)}, "
                  f"tools {tc}){err}")
            for t in b["titles"]:
                print(f"        - {t}")
            print()

    n = len(people)
    print("─" * 64)
    print(f"TOTALS over {n} people")
    print(f"  A search+haiku : {a_verified} verified  | ${a_cost:.5f}  "
          f"(${a_cost/n:.5f}/person)")
    print(f"  B agent        : {b_verified} verified  | ${b_cost:.5f}  "
          f"(${b_cost/n:.5f}/person)")
    if a_cost > 0:
        print(f"  cost ratio     : agent is {b_cost/a_cost:.1f}x the search path")
    BASE = 1056
    print(f"\nExtrapolated to the full base ({BASE} alumni):")
    print(f"  A search+haiku : ${a_cost/n*BASE:,.2f}")
    print(f"  B agent        : ${b_cost/n*BASE:,.2f}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="agent-bakeoff", description=__doc__)
    p.add_argument("--limit", type=int, default=3, help="How many people to sample")
    p.add_argument("--name", default=None, help="One specific person instead of a sample")
    p.add_argument("--model", default="openai/gpt-5-mini", help="Agent model id")
    p.add_argument("--tools", default="people_search,web_search",
                   help="Comma-separated agent tools")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    tools = tuple(t.strip() for t in args.tools.split(",") if t.strip())
    return run(limit=args.limit, name=args.name, model=args.model, tools=tools)


if __name__ == "__main__":
    import config  # noqa: F401  (loads .env)
    sys.exit(main())
