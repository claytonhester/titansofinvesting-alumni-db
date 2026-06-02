"""One-off probe: can Firecrawl's AGENT endpoint pull a LinkedIn profile for a
Titans alumnus, and what does it actually cost?

Plain client.scrape() refuses LinkedIn (WebsiteNotSupportedError). The orgbase
LinkedIn-sync system gets around this with firecrawl.agent({prompt, schema}) — a
browsing model that navigates the page itself and returns structured JSON. This
ports that approach, plus the one step orgbase skips: we don't have the LinkedIn
URL, so we SEARCH for it first (cheap, metadata only) before spending on the
agent.

This is a measurement tool, not pipeline wiring. It runs ONE person, caps spend
with max_credits, and prints the true credit cost (AgentResponse.credits_used +
a get_credit_usage() delta as a cross-check) so we can decide if LinkedIn is
worth folding into phase2_enrich.

    python linkedin_probe.py                       # picks one enriched person
    python linkedin_probe.py --name "Matt Ockwood" --company "Chambers Energy Capital" --city Houston
    python linkedin_probe.py --max-credits 60      # tighten/loosen the spend cap
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from pydantic import BaseModel, Field

import config
from firecrawl import Firecrawl
from firecrawl.v2.types import SearchResultWeb

# Hard ceiling so a runaway agent can't drain the balance. The agent endpoint is
# the priciest call we make (it browses + runs its own web searches); cap it.
DEFAULT_MAX_CREDITS = 60

# ~$0.00083/credit, the rate implied by the measured scrape run (76 cr ≈ $0.063).
USD_PER_CREDIT = 0.063 / 76


# --- Extraction schema (ported from orgbase firecrawl.ts LinkedInProfileSchema,
# trimmed to what the Titans claims table actually stores) -------------------
class _Workplace(BaseModel):
    company_name: str
    job_title: str
    company_size: str | None = None
    industry: str | None = None


class _Experience(BaseModel):
    company_name: str
    job_title: str
    employment_dates: str
    location: str | None = None


class _Location(BaseModel):
    city: str
    state: str | None = None
    country: str | None = None


class LinkedInProfile(BaseModel):
    name: str
    headline: str | None = None
    linkedin_profile_url: str
    current_location: _Location | None = None
    current_workplace: _Workplace | None = None
    work_experience: list[_Experience] = Field(default_factory=list)


_AGENT_PROMPT = """Extract professional information from the LinkedIn profile at: {url}

Extract:
1. name, headline (optional), and the LinkedIn profile URL.
2. Current residential location (city, state, country).
3. Current workplace (CURRENT COMPANY ONLY): company name, job title, and — \
researching the current company via web search if not on the page — approximate \
company size (e.g. "201-500 employees") and primary industry. Use "N/A" if not \
conclusive after research.
4. Complete work experience history: for each role capture job title, company \
name, employment dates, and location.

Only research company size/industry for the CURRENT company. Use "N/A" rather \
than guessing when data is not conclusive."""


def find_linkedin_url(client: Firecrawl, name: str, company: str) -> str | None:
    """Cheap search step orgbase skips (it's handed the URL). Take the top
    linkedin.com/in/ hit for the name+company query."""
    query = f'"{name}" {company} linkedin'
    data = client.search(query, limit=5)
    for item in data.web or []:
        if isinstance(item, SearchResultWeb) and "linkedin.com/in/" in (item.url or ""):
            return item.url
    return None


def pick_person(name: str | None, company: str | None, city: str | None) -> tuple[str, str, str]:
    if name:
        return name, company or "", city or ""
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT full_name, research_company, city FROM people "
            "WHERE COALESCE(research_company,'') <> '' ORDER BY id LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    if not row:
        raise SystemExit("no person with a research_company found; pass --name")
    return row["full_name"], row["research_company"] or "", row["city"] or ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name")
    ap.add_argument("--company")
    ap.add_argument("--city")
    # Both default to OFF — the working orgbase call passes neither. A credit cap
    # makes the agent refuse pre-flight; leave it unset unless deliberately probing.
    ap.add_argument("--max-credits", type=int, default=0, help="0 = unset (recommended).")
    ap.add_argument("--model", default=None, choices=["spark-1-pro", "spark-1-mini"],
                    help="Unset = Firecrawl default (recommended).")
    ap.add_argument(
        "--constrain-urls",
        action="store_true",
        help="Pass urls=[url] to the agent (direct-scrape mode). Default OFF — "
        "orgbase omits urls so the agent browses/web-searches freely instead of "
        "trying to scrape the protected page directly (which LinkedIn refuses).",
    )
    args = ap.parse_args()

    name, company, city = pick_person(args.name, args.company, args.city)
    client = Firecrawl(api_key=config.require_key("FIRECRAWL_API_KEY"))

    print(f"probe: {name} | {company} | {city}")
    print(f"  model={args.model}  max_credits={args.max_credits}")

    url = find_linkedin_url(client, name, company)
    if not url:
        print("  no linkedin.com/in/ URL found in search — agent has nothing to target.")
        return 1
    print(f"  found URL: {url}")

    before = client.get_credit_usage().remaining_credits

    # WORKING CONFIG (matches orgbase): pass ONLY prompt + schema. Omitting urls
    # lets the agent browse/web-search instead of direct-scraping the protected
    # page; omitting max_credits + model is essential — forcing a credit cap makes
    # the agent abort pre-flight with a misleading "reached max credits" refusal.
    agent_kwargs = dict(
        prompt=_AGENT_PROMPT.format(url=url),
        schema=LinkedInProfile,
    )
    if args.model:
        agent_kwargs["model"] = args.model
    if args.max_credits:
        agent_kwargs["max_credits"] = args.max_credits
    if args.constrain_urls:
        agent_kwargs["urls"] = [url]
    mode = "free browse (no urls)" if not args.constrain_urls else "constrained urls=[url]"
    caps = f" model={args.model or 'default'} max_credits={args.max_credits or 'none'}"
    print(f"  mode={mode}{caps}")

    resp = client.agent(**agent_kwargs)

    after = client.get_credit_usage().remaining_credits
    delta = (before - after) if (before is not None and after is not None) else None

    print(f"\n  status={resp.status}  credits_used={resp.credits_used}  balance_delta={delta}")
    if resp.credits_used is not None:
        print(f"  est. cost: ${resp.credits_used * USD_PER_CREDIT:.4f}")
    if resp.error:
        print(f"  error: {resp.error}")

    print("\n=== EXTRACTED PROFILE ===")
    print(json.dumps(resp.data, indent=2, ensure_ascii=False, default=str) if resp.data else "  (no data)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
