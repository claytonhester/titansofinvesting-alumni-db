"""People Data Labs Company Enrichment — a CACHED firm-level layer.

Firmographics are keyed by firm, not person, and shared by every alumnus who works
there. So enrichment is a separate pass that enriches each domain ONCE and caches it
in the `companies` table: the pass skips any domain already present, so we never
spend a credit on the same company twice (free tier: 100 company credits/month).

Two design points mirror pdl_enrich:
- Same auth (`X-Api-Key`) + retry/backoff shape; never raises.
- Free "Company Base" bundle returns fields at the TOP LEVEL of the response
  (unlike person/enrich which nests under `data`). A confident match needs a
  DOMAIN — name-only matches weakly and returns nothing — so we source the firm
  domain from PDL's `job_company_website` (captured on the person) and, for the
  existing cohort, from a firm-bio host in public_links.

Run as: ``python company_enrich.py --limit 100`` after a person wave.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx
from dotenv import load_dotenv

from company_store import (
    CompanyRecord,
    existing_domains,
    get_company,
    init_company_schema,
    upsert_company,
)
from config import DB_PATH, REPO_ROOT
from db import connect
from directory_hosts import DIRECTORY_HOSTS, SOCIAL_HOSTS
from person_company_store import init_person_company_schema, linked_domains
from person_insights_store import init_person_insights_schema

PDL_COMPANY_URL = "https://api.peopledatalabs.com/v5/company/enrich"

# Hosts that are never a firm's own site — the shared broker/social core (see
# directory_hosts.py) plus sources that are real records but still not an employer
# domain (filings, reference, market-data). A public_links host in here is not the
# employer domain.
_NON_FIRM_HOSTS = DIRECTORY_HOSTS | SOCIAL_HOSTS | frozenset({
    "bloomberg.com", "sec.gov", "wikipedia.org",
})

# Generic tokens that don't identify a firm — dropped before matching a host root.
# Includes geographic terms: "texas" must NOT match "texastaxpayers.com" to an
# employer "Teacher Retirement System of Texas" (a real false-positive we hit).
_EMP_STOPWORDS = frozenset({
    "inc", "incorporated", "llc", "lp", "llp", "ltd", "limited", "co", "corp",
    "corporation", "company", "group", "holdings", "the", "and", "of", "&",
    # geographic / ultra-generic
    "texas", "austin", "houston", "dallas", "antonio", "san", "new", "york",
    "california", "francisco", "los", "angeles", "chicago", "boston", "miami",
    "national", "american", "america", "global", "united", "states", "usa",
    "us", "north", "south", "east", "west", "international",
})


def _bare_domain(url_or_host: str) -> str:
    """Canonical bare host: strip scheme/path/www, lowercase. '' if unusable."""
    s = (url_or_host or "").strip().lower()
    if not s:
        return ""
    if "//" not in s:
        s = "//" + s  # let urlparse treat a bare host as netloc
    try:
        host = urlparse(s).hostname or ""
    except Exception:
        return ""
    return host.removeprefix("www.")


def _emp_tokens(employer: str) -> list[str]:
    raw = "".join(c if c.isalnum() or c.isspace() else " " for c in (employer or "").lower())
    return [t for t in raw.split() if t and t not in _EMP_STOPWORDS and len(t) >= 3]


def resolve_employer_domain(employer: str, public_link_urls: list[str]) -> str:
    """Best-effort firm domain from the person's public_links (backfill source for
    people enriched before we captured PDL's job_company_website). Picks a host that
    is NOT an aggregator/social site AND whose root shares a token with the employer
    name, so 'Sage Advisory Services' resolves to sageadvisory.com, not a directory."""
    tokens = _emp_tokens(employer)
    if not tokens:
        return ""
    acronym = "".join(t[0] for t in tokens)  # "Brighton Park Capital" -> "bpc"
    for url in public_link_urls:
        host = _bare_domain(url)
        if not host or host in _NON_FIRM_HOSTS:
            continue
        root = host.split(".")[0]
        # token overlap ("sageadvisory" ~ "Sage Advisory") OR exact acronym match
        # ("bpc" == B/righton P/ark C/apital), kept tight to avoid false firms.
        if any(tok in root or root in tok for tok in tokens):
            return host
        if len(acronym) >= 2 and root == acronym:
            return host
    return ""


def _i(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _s(value: object) -> str:
    return str(value).strip() if value not in (None, "") else ""


def enrich_company(
    client: httpx.Client,
    api_key: str,
    domain: str,
    *,
    attempts: int = 3,
    backoff_base: float = 1.5,
) -> CompanyRecord | None:
    """Enrich one firm by domain. Returns a matched CompanyRecord, a no-match
    SENTINEL (matched=False) when PDL returns 200 with no usable fields (so we
    cache the miss and don't re-pay), or None on a transient outage (so the caller
    leaves it uncached to retry later). Never raises."""
    domain = _bare_domain(domain)
    if not domain or not api_key:
        return None
    headers = {"X-Api-Key": api_key, "Accept": "application/json"}
    params = {"website": domain}
    body: dict | None = None
    for attempt in range(attempts):
        try:
            resp = client.get(PDL_COMPANY_URL, params=params, headers=headers)
        except Exception:
            if attempt == attempts - 1:
                return None
            time.sleep(backoff_base ** attempt)
            continue
        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception:
                return None
            break
        if resp.status_code == 404:
            return CompanyRecord(domain=domain, matched=False)  # genuine no-match
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == attempts - 1:
                return None
            time.sleep(backoff_base ** attempt)
            continue
        return None  # other 4xx: retrying won't help, but don't cache a bad key
    if not isinstance(body, dict):
        return None

    name = _s(body.get("display_name")) or _s(body.get("name"))
    industry = _s(body.get("industry"))
    size = _s(body.get("size"))
    employee_count = _i(body.get("employee_count"))
    # No usable firmographics -> a genuine (cacheable) no-match sentinel.
    if not (name or industry or size or employee_count):
        return CompanyRecord(domain=domain, matched=False, likelihood=_i(body.get("likelihood")))

    location = body.get("location") if isinstance(body.get("location"), dict) else {}
    tags = body.get("tags") if isinstance(body.get("tags"), list) else []
    return CompanyRecord(
        domain=domain,
        name=name,
        industry=industry,
        industry_v2=_s(body.get("industry_v2")),
        size=size,
        employee_count=employee_count,
        company_type=_s(body.get("type")),
        ticker=_s(body.get("ticker")),
        founded=_i(body.get("founded")),
        hq_location=_s((location or {}).get("name")),
        linkedin_url=_s(body.get("linkedin_url")),
        summary=_s(body.get("summary")),
        tags=[_s(t) for t in tags if _s(t)][:10],
        likelihood=_i(body.get("likelihood")),
        matched=True,
    )


def _person_employers(conn) -> list[dict]:
    """Each enriched person with a current_employer, plus their stored
    employer_domain (if any) and their public_links URLs (backfill domain source)."""
    rows = conn.execute(
        """
        SELECT p.id AS person_id,
               MAX(CASE WHEN c.claim_type='current_employer' THEN c.value END) AS employer,
               (SELECT pi.employer_domain FROM person_insights pi WHERE pi.person_id = p.id) AS employer_domain,
               (SELECT GROUP_CONCAT(c2.source_url, '|')
                  FROM claims c2 WHERE c2.person_id = p.id AND c2.claim_type='public_links') AS links
        FROM people p
        JOIN claims c ON c.person_id = p.id
        GROUP BY p.id
        HAVING employer IS NOT NULL AND employer != ''
        """
    ).fetchall()
    out = []
    for r in rows:
        out.append({
            "person_id": r["person_id"],
            "employer": r["employer"] or "",
            "employer_domain": (r["employer_domain"] or "") if "employer_domain" in r.keys() else "",
            "links": [u for u in (r["links"] or "").split("|") if u],
        })
    return out


def run(db_path: str = str(DB_PATH), limit: int = 100) -> int:
    """Cached company-enrichment pass. Resolves each person's firm domain, records
    it on person_insights, then enriches the DISTINCT new domains (skipping any
    already in `companies`) up to `limit`. Idempotent: a second run enriches 0."""
    load_dotenv(REPO_ROOT / ".env", override=True)
    api_key = os.getenv("PDL_API_KEY")
    if not api_key:
        print("PDL_API_KEY not set — cannot enrich companies.", file=sys.stderr)
        return 1

    with connect(Path(db_path)) as conn:
        init_company_schema(conn)
        init_person_insights_schema(conn)  # ensures employer_domain column exists
        init_person_company_schema(conn)

        people = _person_employers(conn)
        # Resolve + persist each person's employer domain.
        want: dict[str, str] = {}  # domain -> an employer name (for logging)
        for p in people:
            domain = _bare_domain(p["employer_domain"]) or resolve_employer_domain(
                p["employer"], p["links"]
            )
            if not domain:
                continue
            if domain != p["employer_domain"]:
                conn.execute(
                    "UPDATE person_insights SET employer_domain = ? WHERE person_id = ?",
                    (domain, p["person_id"]),
                )
            want.setdefault(domain, p["employer"])
        conn.commit()

        # Also enrich firms referenced anywhere in career history (past employers),
        # so their company pages exist for the "previously here" view.
        for d in linked_domains(conn):
            want.setdefault(d, "")

        have = existing_domains(conn)
        todo = [d for d in want if d not in have]   # <-- the cache: never twice
        print(f"{len(want)} distinct firm domains across alumni; "
              f"{len(have)} already cached; {len(todo)} new to enrich "
              f"(limit {limit}).")

        enriched = matched = 0
        with httpx.Client(timeout=30.0) as http:
            for domain in todo[:limit]:
                rec = enrich_company(http, api_key, domain)
                if rec is None:
                    print(f"  {domain}: transient failure — left uncached")
                    continue
                upsert_company(conn, rec)
                conn.commit()
                enriched += 1
                if rec.matched:
                    matched += 1
                    print(f"  ✓ {rec.name}  [{rec.industry or '?'} · {rec.size or '?'} · "
                          f"{rec.hq_location or '?'}]")
                else:
                    print(f"  – {domain}: no PDL match (cached as sentinel)")

        print(f"\nEnriched {enriched} firms ({matched} matched). "
              f"companies table now holds {len(existing_domains(conn))} firms.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="company-enrich", description=__doc__)
    p.add_argument("--db", default=str(DB_PATH))
    p.add_argument("--limit", type=int, default=100,
                   help="max NEW firms to enrich this run (free tier = 100/mo)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(db_path=args.db, limit=args.limit)


if __name__ == "__main__":
    sys.exit(main())
