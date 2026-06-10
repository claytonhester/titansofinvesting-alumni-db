"""Sifting report — a DRY-RUN that shows, per person, what each enrichment
destination WOULD surface, so a human can spot-check precision before a real run.

The discovery funnel is wide (Perplexity baseline + multi-facet/multi-company Sonar),
and the documents that belong in the NEWS feed differ from the ones we LINK on the
profile, which differ again from the RÉSUMÉ facts. This tool prints all three buckets
with KEPT and DROPPED items (and the reason each was dropped) so reviewers confirm
only what matters gets through.

Firecrawl-free and READ-ONLY: it uses the same free Jina baseline + Perplexity +
Haiku/Sonnet path as production but writes NOTHING and spends NO Firecrawl credits.
Run it on a representative sample (e.g. --limit 25) before committing the full run.

    python enrich_report.py --limit 25
    python enrich_report.py --name "Jane Doe"
    python enrich_report.py --class 3 --school "Texas A&M"
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx
from anthropic import Anthropic

from config import DB_PATH, require_key
from cost_log import PDL_USD_PER_MATCH
from db import connect, init_schema
from directory_hosts import DIRECTORY_HOSTS, PUBLIC_RECORDS_HOSTS, SOCIAL_HOSTS, registrable_host
from enrichment_store import ClaimRow, init_enrichment_schema
from http_fetch import fetch_article as fetch_article_jina
from identity import PersonAnchors, accepted_sources, resolve_identity
from identity_prefilter import prefilter
from jina_discovery import discover_via_jina
from mention_discovery import discover_mentions
from news_curate import _is_boilerplate_title, _is_name_only_title, news_items, curate_news
from pdl_enrich import enrich_pdl
from sonar_news import discover_press_sonar
from structuring import structure_profile

# Import the production helpers so the report mirrors the real pipeline exactly.
from phase2_enrich import Person, _claim_rows, _load_targets

_DROP_HOSTS = DIRECTORY_HOSTS | PUBLIC_RECORDS_HOSTS | SOCIAL_HOSTS


@dataclass(frozen=True)
class Bucket:
    """One destination's outcome: what would show vs what was dropped (with reason)."""

    kept: list[str]
    dropped: list[tuple[str, str]]


@dataclass(frozen=True)
class PersonReport:
    name: str
    resume: Bucket
    news: Bucket
    links: Bucket


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.").lower()
    except ValueError:
        return ""


def _canonical(url: str) -> str:
    try:
        u = urlparse(url)
        return f"{(u.hostname or '').removeprefix('www.')}{u.path.rstrip('/')}".lower()
    except ValueError:
        return url.strip().lower()


def classify_links(claims: list[ClaimRow], name: str) -> Bucket:
    """Split public_links into would-show vs dropped (with reason), mirroring the
    web's usefulLinks (link-quality.ts): drop LinkedIn (shown as the header button),
    broker/directory/social/records hosts, bare-URL labels, firm boilerplate, and
    name-only bio headings. De-duplicate by canonical URL."""
    kept: list[str] = []
    dropped: list[tuple[str, str]] = []
    seen: set[str] = set()
    for c in claims:
        if c.claim_type != "public_links":
            continue
        label = (c.value or "").strip()
        url = (c.source_url or "").strip()
        host = _host(url)
        hay = f"{label} {url}".lower()
        if "linkedin.com" in hay:
            dropped.append((label or url, "linkedin (shown as header button)"))
            continue
        if host in _DROP_HOSTS or registrable_host(host) in _DROP_HOSTS:
            dropped.append((label or url, f"broker/directory/social host ({host})"))
            continue
        if label.lower().startswith(("http://", "https://")):
            dropped.append((url, "bare-URL label (no real title)"))
            continue
        if _is_boilerplate_title(label):
            dropped.append((label, "firm boilerplate title"))
            continue
        if name and _is_name_only_title(label, name):
            dropped.append((label, "name-only bio heading"))
            continue
        key = _canonical(url)
        if key in seen:
            dropped.append((label, "duplicate URL"))
            continue
        seen.add(key)
        kept.append(label)
    return Bucket(kept=kept, dropped=dropped)


def _resume_bucket(sources, verdicts) -> Bucket:
    accepted = {s.url for s in accepted_sources(sources, verdicts)}
    kept = sorted(accepted)
    dropped = [
        (v.source_url, f"{v.decision}: {v.reason}")
        for v in verdicts
        if v.source_url not in accepted
    ]
    return Bucket(kept=kept, dropped=dropped)


def _news_bucket(curated, candidates: list[ClaimRow]) -> Bucket:
    shown_urls = {c.source_url for c in curated}
    kept = [f"[{c.category}] {c.headline}" for c in curated]
    dropped = [
        (item.value, "not the subject / below bar (curator + article verify)")
        for item in candidates
        if item.source_url not in shown_urls
    ]
    return Bucket(kept=kept, dropped=dropped)


def build_person_report(
    http: httpx.Client,
    anthropic: Anthropic,
    person: Person,
    *,
    pdl_key: str | None,
    perplexity_key: str | None,
) -> PersonReport:
    """Run the Firecrawl-free path for one person and bucket the outcomes. No writes."""
    anchors = PersonAnchors(
        full_name=person.full_name, company=person.company, city=person.city,
        school=person.school, titan_class=person.titan_class,
    )
    disc = discover_via_jina(http, perplexity_key, person.full_name, person.company, person.city)
    pre = prefilter(anchors, disc.sources)
    identity = resolve_identity(anthropic, anchors, pre.ambiguous)
    verdicts = pre.decided + identity.verdicts
    trusted = accepted_sources(disc.sources, verdicts)
    struct = structure_profile(anthropic, person.full_name, trusted)
    claim_rows = _claim_rows(struct)

    employer = (struct.profile.get("current_employer") or {}).get("value", "") or person.company
    title = (struct.profile.get("current_title") or {}).get("value", "")

    pdl = (
        enrich_pdl(
            http, pdl_key, person.full_name, employer, person.city,
            school=person.school, cost_usd_per_match=PDL_USD_PER_MATCH,
        )
        if pdl_key else None
    )
    industry = pdl.attributes.current_industry if pdl is not None else ""
    past = tuple(dict.fromkeys(
        cl.company_name for cl in (pdl.career_links if pdl is not None else ())
        if cl.company_name and not cl.is_current
    ))
    if pdl is not None and pdl.claim_rows:
        claim_rows.extend(pdl.claim_rows)

    mentions = discover_mentions(
        http, anthropic, person.full_name, employer, person.city, perplexity_key=perplexity_key
    )
    claim_rows.extend(mentions.claim_rows)
    sonar = discover_press_sonar(
        http, person.full_name, employer, person.city, perplexity_key=perplexity_key,
        role=title, industry=industry, past_companies=past,
    )
    claim_rows.extend(sonar.claim_rows)

    candidates = news_items(claim_rows, person.full_name)
    curated, _, _ = curate_news(
        anthropic, person.full_name, employer, claim_rows,
        fetch_article=fetch_article_jina, career=past,
    )

    return PersonReport(
        name=person.full_name,
        resume=_resume_bucket(disc.sources, verdicts),
        news=_news_bucket(curated, candidates),
        links=classify_links(claim_rows, person.full_name),
    )


def _render(report: PersonReport) -> str:
    lines = [f"\n=== {report.name} ==="]
    for title, bucket in (
        ("RÉSUMÉ sources", report.resume),
        ("NEWS feed", report.news),
        ("PROFILE links", report.links),
    ):
        lines.append(f"  {title}: {len(bucket.kept)} shown / {len(bucket.dropped)} dropped")
        for k in bucket.kept:
            lines.append(f"    ✓ {k}")
        for item, reason in bucket.dropped:
            lines.append(f"    ✗ {item}  — {reason}")
    return "\n".join(lines)


def run(limit: int, name: str | None, titan_class: int | None, school: str | None) -> int:
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
    pdl_key = os.getenv("PDL_API_KEY")
    perplexity_key = os.getenv("PERPLEXITY_API_KEY")
    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_enrichment_schema(conn)
        people = _load_targets(conn, limit, name, titan_class, school)
    if not people:
        print("No matching people.", file=sys.stderr)
        return 1
    print(f"Sifting report (DRY RUN, no writes, no Firecrawl) for {len(people)} people")
    with httpx.Client(timeout=30.0) as http:
        for person in people:
            try:
                report = build_person_report(
                    http, anthropic, person,
                    pdl_key=pdl_key, perplexity_key=perplexity_key,
                )
                print(_render(report))
            except Exception as exc:  # noqa: BLE001 - a report must not abort the batch
                print(f"\n=== {person.full_name} ===\n  ERROR: {exc}", file=sys.stderr)
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="enrich-report", description=__doc__)
    p.add_argument("--limit", type=int, default=25, help="How many people to sample")
    p.add_argument("--name", default=None, help="Report on one specific person")
    p.add_argument("--class", dest="titan_class", type=int, default=None, help="Target a Titan class")
    p.add_argument("--school", default=None, help="Restrict --class to one school")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(limit=args.limit, name=args.name, titan_class=args.titan_class, school=args.school)


if __name__ == "__main__":
    sys.exit(main())
