"""Phase 2 enrichment orchestrator: directory row -> persisted profile.

Ties the three Stage-2 pieces together with claim-level provenance and
resumable batch state:

    discover()          Firecrawl search+scrape  -> candidate sources
    resolve_identity()  Sonnet merge gate         -> per-source verdict
    structure_profile() Haiku extraction          -> claims (accepted sources only)

Everything is persisted to the Stage-2 tables (see enrichment_store): sources,
the FULL identity-candidate trail (accept/review/reject), and claims. Each
phase records batch_status so a re-run resumes only unfinished people. Claims
are extracted ONLY from auto-accepted sources — uncertain identities are held
for human review, never auto-merged.

    python phase2_enrich.py --limit 5      # next 5 un-enriched alumni
    python phase2_enrich.py --name "Jane Doe"

Note: this is the production-shaped path and bills both Firecrawl and Claude.
For pure cost measurement on a throwaway sample, use phase2_discover.py.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from dataclasses import dataclass

import httpx
from anthropic import Anthropic
from firecrawl import Firecrawl

from config import DB_PATH, require_key
from cost_log import PDL_USD_PER_MATCH, append_entry, build_entry, remaining_credits
from gnews_enrich import fetch_news
from pdl_enrich import enrich_pdl
from db import connect, init_schema
from discovery import DiscoveryResult, Source, _domain, discover
from enrichment_store import (
    DECISION_ACCEPT,
    DECISION_REVIEW,
    PHASE_IDENTITY,
    PHASE_STRUCTURING,
    CandidateRow,
    ClaimRow,
    SourceRow,
    init_enrichment_schema,
    mark_phase,
    pending_people,
    replace_candidates,
    replace_claims,
    replace_sources,
)
from identity import (
    IdentityVerdict,
    PersonAnchors,
    accepted_sources,
    resolve_identity,
)
from identity_prefilter import prefilter
from structuring import (
    BIO_SYNTHESIS_METHOD,
    HAIKU_MODEL,
    StructuringResult,
    structure_profile,
    synthesize_bio,
)


@dataclass(frozen=True)
class _PersonUsage:
    """Token / credit usage for one person, accumulated into the run cost log."""

    credits: int
    haiku_in: int
    haiku_out: int
    sonnet_in: int
    sonnet_out: int
    pdl_matches: int
    pdl_usd: float
    gnews_requests: int


@dataclass(frozen=True)
class Person:
    id: int
    full_name: str
    company: str
    city: str
    school: str
    titan_class: int


def _load_targets(
    conn: sqlite3.Connection, limit: int, name: str | None
) -> list[Person]:
    if name:
        rows = conn.execute(
            "SELECT id, full_name, initial_company, city, school, titan_class "
            "FROM people WHERE full_name = ?",
            (name,),
        ).fetchall()
    else:
        ids = pending_people(conn, PHASE_STRUCTURING, limit)
        if not ids:
            return []
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT id, full_name, initial_company, city, school, titan_class "
            f"FROM people WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
    return [
        Person(
            id=r["id"],
            full_name=r["full_name"],
            company=r["initial_company"],
            city=r["city"],
            school=r["school"],
            titan_class=r["titan_class"],
        )
        for r in rows
    ]


def _anchors(person: Person) -> PersonAnchors:
    return PersonAnchors(
        full_name=person.full_name,
        company=person.company,
        city=person.city,
        school=person.school,
        titan_class=person.titan_class,
    )


def _source_rows(disc: DiscoveryResult) -> list[SourceRow]:
    return [
        SourceRow(url=s.url, domain=_domain(s.url), title=s.title, relevance=s.relevance)
        for s in disc.sources
    ]


def _candidate_rows(
    verdicts: tuple[IdentityVerdict, ...], model: str
) -> list[CandidateRow]:
    return [
        CandidateRow(
            source_url=v.source_url,
            confidence=v.confidence,
            decision=v.decision,
            reason=v.reason,
            model=model,
        )
        for v in verdicts
    ]


def _claim_rows(struct: StructuringResult) -> list[ClaimRow]:
    """Flatten the structured profile into the claim_provenance grain. Each
    {value, confidence, source_url, quote} field becomes one claim row; list
    fields (career_history, education, public_links) emit one row per entry."""
    rows: list[ClaimRow] = []
    for claim_type, node in struct.profile.items():
        if isinstance(node, dict):
            row = _claim_from_field(claim_type, node)
            if row is not None:
                rows.append(row)
        elif isinstance(node, list):
            for entry in node:
                if isinstance(entry, dict):
                    row = _claim_from_field(claim_type, entry)
                    if row is not None:
                        rows.append(row)
    return rows


def _claim_from_field(claim_type: str, field: dict) -> ClaimRow | None:
    value = field.get("value")
    if value is None:
        return None
    try:
        confidence = float(field.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return ClaimRow(
        claim_type=claim_type,
        value=str(value),
        source_url=str(field.get("source_url") or ""),
        quote=str(field.get("quote") or ""),
        confidence=confidence,
        extraction_method=HAIKU_MODEL,
    )


def enrich_person(
    conn: sqlite3.Connection,
    firecrawl: Firecrawl,
    anthropic: Anthropic,
    person: Person,
    http: httpx.Client,
    pdl_key: str | None,
    gnews_key: str | None,
) -> _PersonUsage:
    """Run the full pipeline for one person and persist every stage. Records
    batch_status per phase so a crash mid-batch resumes cleanly. Returns the
    person's usage so the caller can fold it into the run-level cost log."""
    disc = discover(firecrawl, person.full_name, person.company, person.city)
    replace_sources(conn, person.id, _source_rows(disc))
    anchors = _anchors(person)

    # Deterministic pre-filter first: slam-dunk multi-anchor matches are accepted
    # without Sonnet; only the ambiguous remainder is sent to the (billed) gate.
    pre = prefilter(anchors, disc.sources)
    identity = resolve_identity(anthropic, anchors, pre.ambiguous)
    verdicts = pre.decided + identity.verdicts
    candidate_rows = _candidate_rows(pre.decided, "prefilter") + _candidate_rows(
        identity.verdicts, "sonnet"
    )
    replace_candidates(conn, person.id, candidate_rows)
    mark_phase(conn, person.id, PHASE_IDENTITY, "done")

    trusted: tuple[Source, ...] = accepted_sources(disc.sources, verdicts)
    struct = structure_profile(anthropic, person.full_name, trusted)
    claim_rows = _claim_rows(struct)

    # When no source handed us a ready-made narrative, compose a short_bio from
    # the facts we DID verify — so "enough is known" produces a description even
    # when no page wrote one. Tagged as synthesis, never a direct quote.
    bio = synthesize_bio(anthropic, person.full_name, struct.profile)
    if bio is not None:
        claim_rows.append(
            ClaimRow(
                claim_type="short_bio",
                value=bio.value,
                source_url="",
                quote="",
                confidence=bio.confidence,
                extraction_method=BIO_SYNTHESIS_METHOD,
            )
        )

    # PDL deepens the verified résumé (canonical claim_types, identity-gated on
    # likelihood); GNews adds unverified news_mention rows kept separate on the
    # web side. Both skip cleanly when their key is unset and never raise, so a
    # missing key or an outage degrades enrichment instead of aborting it.
    pdl = (
        enrich_pdl(
            http,
            pdl_key,
            person.full_name,
            person.company,
            person.city,
            cost_usd_per_match=PDL_USD_PER_MATCH,
        )
        if pdl_key
        else None
    )
    if pdl is not None:
        claim_rows.extend(pdl.claim_rows)

    news = fetch_news(http, gnews_key, person.full_name) if gnews_key else None
    if news is not None:
        claim_rows.extend(news.claim_rows)

    replace_claims(conn, person.id, claim_rows)
    mark_phase(conn, person.id, PHASE_STRUCTURING, "done")

    n_accept = sum(1 for v in verdicts if v.decision == DECISION_ACCEPT)
    n_review = sum(1 for v in verdicts if v.decision == DECISION_REVIEW)
    n_pre = len(pre.decided)
    pdl_matched = bool(pdl and pdl.matched)
    n_pdl_claims = len(pdl.claim_rows) if pdl else 0
    n_news = len(news.claim_rows) if news else 0
    print(
        f"  {person.full_name}: {len(disc.sources)} sources -> "
        f"{n_accept} accepted ({n_pre} by pre-filter), {n_review} to review; "
        f"{len(claim_rows)} claims{' (+synth bio)' if bio else ''}"
        f"{f' (+{n_pdl_claims} PDL)' if n_pdl_claims else ''}"
        f"{f' (+{n_news} news)' if n_news else ''}; "
        f"{len(pre.ambiguous)} sent to Sonnet; {disc.credits_spent} credits"
    )
    return _PersonUsage(
        credits=disc.credits_spent,
        haiku_in=struct.input_tokens + (bio.input_tokens if bio else 0),
        haiku_out=struct.output_tokens + (bio.output_tokens if bio else 0),
        sonnet_in=identity.input_tokens,
        sonnet_out=identity.output_tokens,
        pdl_matches=1 if pdl_matched else 0,
        pdl_usd=pdl.cost_usd if pdl else 0.0,
        gnews_requests=news.request_count if news else 0,
    )


def run(limit: int, name: str | None) -> int:
    firecrawl = Firecrawl(api_key=require_key("FIRECRAWL_API_KEY"))
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))

    # PDL/GNews keys are SOFT: their absence simply skips that source so existing
    # runs keep working before the keys are funded. Each adapter is also billed
    # (PDL per match) or rate-limited (GNews), so we read them once per run.
    pdl_key = os.getenv("PDL_API_KEY")
    gnews_key = os.getenv("GNEWS_API_KEY")

    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_enrichment_schema(conn)
        people = _load_targets(conn, limit, name)
        if not people:
            print("Nothing to enrich (all targets done or none matched).", file=sys.stderr)
            return 1

        # Authoritative Firecrawl cost: snapshot the live meter around the batch.
        credits_before = remaining_credits(firecrawl)
        est_credits = 0
        haiku_in = haiku_out = sonnet_in = sonnet_out = 0
        pdl_matches = gnews_requests = 0
        processed = 0

        with httpx.Client(timeout=30.0) as http:
            for person in people:
                print(f"\n=== {person.full_name} | {person.company} | {person.city} ===")
                try:
                    usage = enrich_person(
                        conn, firecrawl, anthropic, person, http, pdl_key, gnews_key
                    )
                    conn.commit()  # persist each person before moving on (resumable)
                    est_credits += usage.credits
                    haiku_in += usage.haiku_in
                    haiku_out += usage.haiku_out
                    sonnet_in += usage.sonnet_in
                    sonnet_out += usage.sonnet_out
                    pdl_matches += usage.pdl_matches
                    gnews_requests += usage.gnews_requests
                    processed += 1
                except Exception as exc:  # noqa: BLE001 - record and continue the batch
                    conn.rollback()
                    mark_phase(
                        conn, person.id, PHASE_STRUCTURING, "error",
                        last_error=str(exc), increment_retry=True,
                    )
                    conn.commit()
                    print(f"  ERROR: {exc}", file=sys.stderr)

    credits_after = remaining_credits(firecrawl)
    entry = build_entry(
        label=name or f"enrich-{processed}",
        people=processed,
        haiku_in=haiku_in,
        haiku_out=haiku_out,
        sonnet_in=sonnet_in,
        sonnet_out=sonnet_out,
        credits_before=credits_before,
        credits_after=credits_after,
        estimated_credits=est_credits,
        pdl_matches=pdl_matches,
        gnews_requests=gnews_requests,
    )
    append_entry(entry)
    if processed:
        src = "estimated" if entry.firecrawl_credits_estimated else "measured"
        print(
            f"\nRun cost ({src}): ${entry.total_usd:.4f} for {processed} people "
            f"(${entry.total_usd / processed:.4f}/person) -> data/cost_log.jsonl"
        )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phase2-enrich", description=__doc__)
    p.add_argument("--limit", type=int, default=5, help="How many un-enriched alumni to process")
    p.add_argument("--name", default=None, help="Enrich one specific person by full name")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(limit=args.limit, name=args.name)


if __name__ == "__main__":
    sys.exit(main())
