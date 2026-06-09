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
from datetime import datetime, timezone

import httpx
from anthropic import Anthropic
from firecrawl import Firecrawl

from config import DB_PATH, require_key
from cost_log import PDL_USD_PER_MATCH, append_entry, build_entry, remaining_credits
from mention_discovery import discover_mentions
from news_enrich import NewsEnrichResult, extract_news_mentions
from discovery import DiscoveryResult, NewsDiscoveryResult, Source, _domain, discover, discover_news
from firecrawl.v2.utils.error_handler import PaymentRequiredError
from normalize import digest_claims
from profile_cleanup import clean_profile
from linkedin_firecrawl import (
    LinkedInBudget,
    agent_batch_budget,
    fetch_linkedin,
)
from pdl_enrich import PdlAttributes, enrich_pdl
from pdl_verify import verify_pdl_claims
from reconcile import reconcile_claims
from career_analysis import (
    first_post_grad_employer,
    num_employers,
    tenure_years,
    years_to_md,
)
from grad_year import derive_grad_year
from kpi_classify import MODEL_METHOD as KPI_METHOD, classify_kpis
from profile_metrics import has_advanced_degree, left_texas
from sector_classify import classify_sector
from news_curate import curate_news
from news_store import init_news_schema, replace_curated_news
from person_insights_store import (
    PersonInsight,
    init_person_insights_schema,
    upsert_person_insight,
)
from db import connect, init_schema
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
    profile_from_claims,
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
    fc_news_credits: int       # Firecrawl credits spent on the news-specific pass
    fc_news_articles: int      # confirmed news mentions found via Firecrawl+Claude
    perplexity_requests: int   # Perplexity /search calls (mention discovery), 1/person


@dataclass(frozen=True)
class Person:
    id: int
    full_name: str
    company: str
    city: str
    school: str
    titan_class: int


def _load_targets(
    conn: sqlite3.Connection,
    limit: int,
    name: str | None,
    titan_class: int | None = None,
    school: str | None = None,
) -> list[Person]:
    if name:
        rows = conn.execute(
            "SELECT id, full_name, initial_company, city, school, titan_class "
            "FROM people WHERE full_name = ?",
            (name,),
        ).fetchall()
    elif titan_class is not None:
        # Target an un-enriched cohort by class (optionally one school) — used to
        # run a representative batch (e.g. A&M class 3) rather than arbitrary
        # pending rows. Resumable: already-done people are excluded.
        clauses = ["p.titan_class = ?"]
        params: list[object] = [titan_class]
        if school:
            clauses.append("p.school = ?")
            params.append(school)
        params.append(limit)
        rows = conn.execute(
            "SELECT p.id, p.full_name, p.initial_company, p.city, p.school, "
            "p.titan_class FROM people p WHERE "
            + " AND ".join(clauses)
            + " AND NOT EXISTS (SELECT 1 FROM batch_status b WHERE "
            "b.person_id = p.id AND b.phase = 'structuring' AND b.status = 'done') "
            "ORDER BY p.id LIMIT ?",
            params,
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
    perplexity_key: str | None = None,
    *,
    li_budget: LinkedInBudget | None = None,
) -> _PersonUsage:
    """Run the full pipeline for one person and persist every stage. Records
    batch_status per phase so a crash mid-batch resumes cleanly. Returns the
    person's usage so the caller can fold it into the run-level cost log."""
    # A standalone call (no batch) gets a fresh single-person agent budget so the
    # LinkedIn gate still works; run() passes a shared budget across the batch.
    if li_budget is None:
        li_budget = LinkedInBudget(agent_batch_budget(1))
    # Firecrawl is the deepest career source but it is billed and can run dry.
    # Treat a 0-credit state as a graceful skip, NOT a fatal abort: PDL +
    # Perplexity remain a working spine, so the batch keeps producing profiles
    # (just without scraped career pages) instead of dying on person one.
    try:
        disc = discover(firecrawl, person.full_name, person.company, person.city)
    except PaymentRequiredError:
        print("  Firecrawl: no credits — career discovery skipped "
              "(using PDL + Perplexity only)")
        disc = DiscoveryResult(
            full_name=person.full_name, sources=(), queries=(), credits_spent=0
        )
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

    # What Claude actually verified as the current role — used to anchor the PDL
    # identity gate and to build sharper news/mention queries below.
    _verified_employer = (struct.profile.get("current_employer") or {}).get("value", "")
    _verified_title = (struct.profile.get("current_title") or {}).get("value", "")

    # PDL deepens the verified résumé (canonical claim_types, identity-gated on
    # likelihood). Skips cleanly when its key is unset and never raises, so a
    # missing key or an outage degrades enrichment instead of aborting it.
    #
    # Anchor on the VERIFIED current employer when we have one, not the roster's
    # initial_company — for an older class the roster company is ~15-20 years stale
    # and matches PDL's current record poorly (the likely cause of low match rate).
    # Falls back to the roster company for thin/empty profiles where structuring
    # produced no employer.
    pdl_company = _verified_employer or person.company
    pdl = (
        enrich_pdl(
            http,
            pdl_key,
            person.full_name,
            pdl_company,
            person.city,
            school=person.school,
            cost_usd_per_match=PDL_USD_PER_MATCH,
        )
        if pdl_key
        else None
    )
    # Identity-gate PDL's career/education extras through Haiku before trusting
    # them. PDL already cleared its likelihood gate, but a confident match can
    # still splice in a namesake's stray entry; this holds the deeper résumé facts
    # to the same bar as our public mentions. Current role/location/links pass
    # through. Conservative — drops only clear inconsistencies, never raises.
    pdl_pv_in = pdl_pv_out = 0
    pdl_dropped = 0
    if pdl is not None and pdl.claim_rows:
        n_before = len(pdl.claim_rows)
        kept_pdl, pdl_pv_in, pdl_pv_out = verify_pdl_claims(
            anthropic, person.full_name,
            _verified_employer or person.company, person.city,
            list(pdl.claim_rows),
        )
        pdl_dropped = n_before - len(kept_pdl)
        claim_rows.extend(kept_pdl)

    # Firecrawl agent-mode LinkedIn — a CORE source, but GAP-FILLING, not blanket.
    # Plain scrape is auth-walled out of LinkedIn; the agent reads the public
    # profile. The agent is billed and variable (observed 45–324 credits/call) and
    # the single biggest line item in a run, so it's triple-gated by LinkedInBudget:
    #   1. profile must be thin (PDL already-rich profiles would just get a dup),
    #   2. the person must have >=1 identity-verified source (firing the name-based
    #      agent on someone with no web footprint almost always finds nothing), and
    #   3. the batch must have agent budget left (hard cap; Firecrawl ignores the
    #      per-call max_credits, so this pre-flight check is the real protection).
    # When it does run, the reconciler merges it with PDL + scrape.
    li_credits = 0
    n_li = 0
    decision = li_budget.decide(claim_rows, len(trusted))
    if not decision.fire:
        print(f"  Firecrawl LinkedIn: skipped ({decision.reason})")
    else:
        try:
            li = fetch_linkedin(
                firecrawl, person.full_name,
                employer=_verified_employer or person.company, city=person.city,
            )
            li_credits = li.credits_used
            li_budget.charge(li_credits)
            n_li = len(li.claim_rows)
            if li.claim_rows:
                claim_rows.extend(li.claim_rows)
        except PaymentRequiredError:
            print("  Firecrawl LinkedIn: no credits — skipped")

    # Compose a short_bio from the verified facts when no source handed us a
    # ready-made narrative. Built from the FULL résumé set (Firecrawl + PDL), so a
    # PDL-matched person with zero scraped pages still gets a description. Composes
    # only from structured facts (never new knowledge); tagged synthesis, not a quote.
    bio = synthesize_bio(anthropic, person.full_name, profile_from_claims(claim_rows))
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

    # Firecrawl + Claude news pass: search press/finance domains, scrape the
    # top hits, and use Haiku to verify identity and extract the mention. Queries
    # use the verified employer/title (set above) so they reflect what Claude
    # found, not the raw directory string — more relevant hits, fewer wasted
    # scrape credits.
    try:
        news_disc = discover_news(
            firecrawl,
            person.full_name,
            person.company,
            verified_employer=_verified_employer,
            verified_title=_verified_title,
        )
    except PaymentRequiredError:
        news_disc = NewsDiscoveryResult(sources=(), credits_spent=0)
    fc_news = extract_news_mentions(anthropic, person.full_name, _verified_employer or person.company, news_disc)
    if fc_news.claim_rows:
        claim_rows.extend(fc_news.claim_rows)

    # Perplexity search + Haiku identity check: confirmed public mentions (bios,
    # profiles, regulatory records). Stored as public_links. Key-gated and
    # never-raises, so it skips cleanly when PERPLEXITY_API_KEY is unset. Uses the
    # verified employer/title when known for a sharper query than the raw directory
    # company.
    mentions = discover_mentions(
        http, anthropic, person.full_name,
        _verified_employer or person.company, person.city,
        perplexity_key=perplexity_key,
    )
    if mentions.claim_rows:
        claim_rows.extend(mentions.claim_rows)

    # LLM reconciliation: the sources disagree on phrasing/freshness, so before
    # the deterministic digest, let Haiku merge same-real-world-fact résumé claims
    # (PDL's "Analyst, TRS" + Firecrawl's "Investment Analyst at Teacher
    # Retirement System (2015–2018)" → one canonical entry) and pick a single
    # current employer/title. Mentions pass through untouched. Never invents,
    # never drops, never raises — so digest still runs on whatever it returns.
    reconciled, rec_in, rec_out = reconcile_claims(anthropic, person.full_name, claim_rows)

    # Normalize casing and deduplicate before persisting — ensures "senior
    # investment manager" and "Senior Investment Manager" from two sources
    # collapse to one properly-cased entry.
    digested = digest_claims(reconciled)
    # Deterministic cleanup the probabilistic reconciler can't be trusted to do:
    # one current role, no student-program / volunteer / personal-site careers, no
    # employer-as-title. Pure; never raises.
    clean_rows = clean_profile(digested)
    replace_claims(conn, person.id, clean_rows)

    # Per-person insights classification (Phase 2.5). Derive grad year (education
    # year when present, else the school-aware Titan-class map), the first
    # post-grad employer, then ask Haiku for the four cohort KPIs. The MD
    # fair-shot rule is NOT applied here — this stores the per-person truth; the
    # Phase-3 roll-up decides how to fold it. Never raises (classifier falls back
    # to a deterministic keyword classification).
    edu_texts = [
        f"{c.value} {c.quote}".strip()
        for c in clean_rows
        if c.claim_type == "education"
    ]
    gy = derive_grad_year(person.school, person.titan_class, edu_texts)
    first_employer = first_post_grad_employer(clean_rows, gy.year)
    flags, kpi_in, kpi_out = classify_kpis(
        anthropic, person.full_name, gy.year, first_employer, clean_rows
    )

    # Collected PDL attributes (already paid for) + derived metrics. PDL attrs are
    # present only on a confident match; everything degrades to empty/None.
    pdl_attrs = pdl.attributes if pdl is not None else PdlAttributes()
    ref_year = datetime.now(timezone.utc).year
    current_employer_val = next(
        (c.value for c in clean_rows if c.claim_type == "current_employer"), ""
    )
    current_location_val = next(
        (c.value for c in clean_rows if c.claim_type == "location"), ""
    )
    upsert_person_insight(
        conn,
        PersonInsight(
            person_id=person.id,
            grad_year=gy.year,
            grad_year_source=gy.source,
            first_employer=first_employer,
            on_buy_side=flags.on_buy_side,
            reached_md=flags.reached_md,
            founder_partner=flags.founder_partner,
            still_first_firm=flags.still_first_firm,
            started_sell_side=flags.started_sell_side,
            current_industry=pdl_attrs.current_industry,
            current_company_size=pdl_attrs.current_company_size,
            job_function=pdl_attrs.job_function,
            job_sub_function=pdl_attrs.job_sub_function,
            pdl_seniority=pdl_attrs.pdl_seniority,
            current_role_start_year=pdl_attrs.current_role_start_year,
            years_experience=pdl_attrs.years_experience,
            linkedin_connections=pdl_attrs.linkedin_connections,
            tenure_years=tenure_years(pdl_attrs.current_role_start_year, ref_year),
            # Keep years_to_md consistent with the reached_md KPI: a velocity
            # figure is only meaningful when we actually credit the MD milestone.
            # (A career title may read "MD" while the classifier declines it.)
            years_to_md=years_to_md(clean_rows, gy.year) if flags.reached_md else None,
            num_employers=num_employers(clean_rows),
            has_advanced_degree=has_advanced_degree(edu_texts),
            current_sector=classify_sector(current_employer_val),
            left_texas=left_texas(current_location_val),
            model=KPI_METHOD,
        ),
    )
    _kpi_tags = [
        name for name, on in (
            ("buy-side", flags.on_buy_side), ("MD+", flags.reached_md),
            ("founder/partner", flags.founder_partner),
            ("first-firm", flags.still_first_firm),
        ) if on
    ]
    print(
        f"  insights: grad {gy.year or '?'} ({gy.source or 'unknown'}), "
        f"first @ {first_employer or '?'}; "
        f"KPIs: {', '.join(_kpi_tags) if _kpi_tags else 'none'}"
    )

    # Curate this person's press mentions into a categorized, summarized, ranked
    # feed (one Haiku call). Never raises; falls back to neutral category + the
    # scraped snippet so the feed still populates.
    curated, news_in, news_out = curate_news(
        anthropic, person.full_name, _verified_employer or person.company, clean_rows
    )
    replace_curated_news(conn, person.id, curated)

    mark_phase(conn, person.id, PHASE_STRUCTURING, "done")

    n_accept = sum(1 for v in verdicts if v.decision == DECISION_ACCEPT)
    n_review = sum(1 for v in verdicts if v.decision == DECISION_REVIEW)
    n_pre = len(pre.decided)
    pdl_matched = bool(pdl and pdl.matched)
    n_pdl_claims = (len(pdl.claim_rows) - pdl_dropped) if pdl else 0
    n_mentions = len(mentions.claim_rows)
    n_fc_news = len(fc_news.claim_rows)
    print(
        f"  {person.full_name}: {len(disc.sources)} sources -> "
        f"{n_accept} accepted ({n_pre} by pre-filter), {n_review} to review; "
        f"{len(claim_rows)} claims{' (+synth bio)' if bio else ''}"
        f"{f' (+{n_pdl_claims} PDL)' if n_pdl_claims else ''}"
        f"{f' (-{pdl_dropped} PDL gated)' if pdl_dropped else ''}"
        f"{f' (+{n_li} LinkedIn)' if n_li else ''}"
        f"{f' (+{n_fc_news} press)' if n_fc_news else ''}"
        f"{f' (+{n_mentions} verified mentions)' if n_mentions else ''}; "
        f"{len(pre.ambiguous)} sent to Sonnet; "
        f"{disc.credits_spent + li_credits + news_disc.credits_spent} credits total"
    )
    return _PersonUsage(
        credits=disc.credits_spent + li_credits,
        haiku_in=(
            struct.input_tokens
            + (bio.input_tokens if bio else 0)
            + fc_news.input_tokens
            + rec_in
            + pdl_pv_in
            + kpi_in
            + news_in
        ),
        haiku_out=(
            struct.output_tokens
            + (bio.output_tokens if bio else 0)
            + fc_news.output_tokens
            + rec_out
            + pdl_pv_out
            + kpi_out
            + news_out
        ),
        sonnet_in=identity.input_tokens,
        sonnet_out=identity.output_tokens,
        pdl_matches=1 if pdl_matched else 0,
        pdl_usd=pdl.cost_usd if pdl else 0.0,
        fc_news_credits=news_disc.credits_spent,
        fc_news_articles=n_fc_news,
        perplexity_requests=mentions.perplexity_requests,
    )


def run(
    limit: int,
    name: str | None,
    titan_class: int | None = None,
    school: str | None = None,
) -> int:
    firecrawl = Firecrawl(api_key=require_key("FIRECRAWL_API_KEY"))
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))

    # PDL / Perplexity keys are SOFT: their absence simply skips that source so
    # existing runs keep working before the keys are funded. Both are billed
    # (PDL per match, Perplexity per search), so we read them once per run.
    pdl_key = os.getenv("PDL_API_KEY")
    perplexity_key = os.getenv("PERPLEXITY_API_KEY")

    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_enrichment_schema(conn)
        init_person_insights_schema(conn)
        init_news_schema(conn)
        people = _load_targets(conn, limit, name, titan_class, school)
        if not people:
            print("Nothing to enrich (all targets done or none matched).", file=sys.stderr)
            return 1

        # Authoritative Firecrawl cost: snapshot the live meter around the batch.
        credits_before = remaining_credits(firecrawl)
        # Hard run-level ceiling on the (unpredictable, sometimes-spiking) LinkedIn
        # agent — the single biggest credit line item. Shared across the batch so
        # spend is bounded regardless of how many profiles come back thin.
        li_budget = LinkedInBudget(agent_batch_budget(len(people)))
        print(f"LinkedIn agent budget for this run: {li_budget.remaining} credits")
        est_credits = 0
        haiku_in = haiku_out = sonnet_in = sonnet_out = 0
        pdl_matches = 0
        fc_news_credits = fc_news_articles = 0
        perplexity_requests = 0
        processed = 0

        with httpx.Client(timeout=30.0) as http:
            for person in people:
                print(f"\n=== {person.full_name} | {person.company} | {person.city} ===")
                try:
                    usage = enrich_person(
                        conn, firecrawl, anthropic, person, http, pdl_key, perplexity_key,
                        li_budget=li_budget,
                    )
                    conn.commit()  # persist each person before moving on (resumable)
                    est_credits += usage.credits + usage.fc_news_credits
                    haiku_in += usage.haiku_in
                    haiku_out += usage.haiku_out
                    sonnet_in += usage.sonnet_in
                    sonnet_out += usage.sonnet_out
                    pdl_matches += usage.pdl_matches
                    fc_news_credits += usage.fc_news_credits
                    fc_news_articles += usage.fc_news_articles
                    perplexity_requests += usage.perplexity_requests
                    processed += 1
                except PaymentRequiredError:
                    # No Firecrawl credits — abort the entire run immediately.
                    # Continuing would just re-raise for every remaining person.
                    print(
                        "\n\nFIRECRAWL CREDITS EXHAUSTED — run aborted.\n"
                        "Top up your credits at https://firecrawl.dev then re-run.\n"
                        "Already-processed people are saved and will be skipped on re-run.",
                        file=sys.stderr,
                    )
                    break
                except Exception as exc:  # noqa: BLE001 - record and continue the batch
                    conn.rollback()
                    mark_phase(
                        conn, person.id, PHASE_STRUCTURING, "error",
                        last_error=str(exc), increment_retry=True,
                    )
                    conn.commit()
                    print(f"  ERROR: {exc}", file=sys.stderr)

    credits_after = remaining_credits(firecrawl)
    batch_label = name or (
        f"{school or 'class'}-{titan_class}" if titan_class is not None
        else f"enrich-{processed}"
    )
    entry = build_entry(
        label=batch_label,
        people=processed,
        haiku_in=haiku_in,
        haiku_out=haiku_out,
        sonnet_in=sonnet_in,
        sonnet_out=sonnet_out,
        credits_before=credits_before,
        credits_after=credits_after,
        estimated_credits=est_credits,
        pdl_matches=pdl_matches,
        perplexity_requests=perplexity_requests,
    )
    append_entry(entry)
    if processed:
        src = "estimated" if entry.firecrawl_credits_estimated else "measured"
        print(
            f"\nRun cost ({src}): ${entry.total_usd:.4f} for {processed} people "
            f"(${entry.total_usd / processed:.4f}/person) -> data/cost_log.jsonl"
        )
        if fc_news_articles:
            print(
                f"Press news pass: {fc_news_articles} verified articles found "
                f"across {processed} people "
                f"({fc_news_credits} Firecrawl credits)"
            )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phase2-enrich", description=__doc__)
    p.add_argument("--limit", type=int, default=5, help="How many un-enriched alumni to process")
    p.add_argument("--name", default=None, help="Enrich one specific person by full name")
    p.add_argument("--class", dest="titan_class", type=int, default=None,
                   help="Target an un-enriched cohort by Titan class number")
    p.add_argument("--school", default=None,
                   help="Restrict --class to one school (e.g. 'Texas A&M')")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(limit=args.limit, name=args.name,
               titan_class=args.titan_class, school=args.school)


if __name__ == "__main__":
    sys.exit(main())
