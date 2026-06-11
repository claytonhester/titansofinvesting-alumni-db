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
import re
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
from news_enrich import extract_news_mentions
from discovery import DiscoveryResult, NewsDiscoveryResult, Source, _domain, discover, discover_news
from firecrawl.v2.utils.error_handler import PaymentRequiredError
from normalize import digest_claims
from profile_cleanup import clean_profile
from linkedin_firecrawl import (
    LinkedInBudget,
    LinkedInDecision,
    _current_role_start_year_from_claims,
    agent_batch_budget,
    fetch_linkedin,
)
from linkedin_verify import verify_linkedin_profile
from linkedin_search import choose_linkedin_url, search_linkedin_candidates
from research_policy import (
    ResearchPolicy,
    bypass_linkedin_gap_gate,
    force_deep_path,
)
from pdl_enrich import PdlAttributes, PdlUnavailable, enrich_pdl
from pdl_verify import verify_pdl_claims
from reconcile import reconcile_claims
from career_analysis import (
    first_post_grad_employer,
    num_employers,
    tenure_years,
    years_to_md,
)
from grad_year import derive_grad_year, grad_year_from_class
from kpi_classify import MODEL_METHOD as KPI_METHOD, classify_kpis
from profile_metrics import has_advanced_degree, left_texas
from sector_classify import classify_sector
from company_enrich import _bare_domain as _company_domain
from deep_gate import FirecrawlBudget, is_high_signal
from http_fetch import fetch_article as fetch_article_jina
from jina_discovery import discover_via_jina
from news_curate import curate_news
from news_store import init_news_schema, replace_curated_news
from sonar_news import discover_press_sonar
from person_company_store import (
    PersonCompany,
    init_person_company_schema,
    replace_person_companies,
)
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
    upsert_candidate,
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
    sonar_requests: int        # Perplexity Sonar press-discovery calls, 1/person
    sonar_usd: float           # Sonar dollar cost (authoritative usage.cost when given)


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
    ids: list[int] | None = None,
    needs_deep: bool = False,
) -> list[Person]:
    if needs_deep:
        # The deep pass of the two-pass run: people the base sweep flagged as
        # still-thin (person_insights.needs_deep_search=1). Like --ids, this is a
        # targeted re-research pass, so it deliberately re-runs already-done
        # people (replace_* rebuilds their profile). The flag self-clears at the
        # next finalize once a profile is rich, so the queue drains.
        rows = conn.execute(
            "SELECT p.id, p.full_name, p.initial_company, p.city, p.school, "
            "p.titan_class FROM people p "
            "JOIN person_insights pi ON pi.person_id = p.id "
            "WHERE pi.needs_deep_search = 1 ORDER BY p.id LIMIT ?",
            (limit,),
        ).fetchall()
    elif ids:
        # Explicit person IDs: a targeted (re-)research pass. Deliberately does NOT
        # exclude already-done people — re-running rebuilds their profile via the
        # replace_* persistence, which is the point of a deep pass.
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            "SELECT id, full_name, initial_company, city, school, titan_class "
            f"FROM people WHERE id IN ({placeholders}) ORDER BY id",
            ids,
        ).fetchall()
    elif name:
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


@dataclass(frozen=True)
class _LinkedInPass:
    """Outcome of one agent fetch + verifier judgment."""
    claim_rows: tuple[ClaimRow, ...]  # EMPTY unless the verifier said verified
    verified_employer: str
    credits: int
    verify_in: int
    verify_out: int
    attempted: bool


_LI_NOT_ATTEMPTED = _LinkedInPass((), "", 0, 0, 0, False)

_LINKEDIN_IN_RE = re.compile(r"https?://[^\s)\"']*linkedin\.com/in/[^\s)\"']+", re.I)


def _candidate_linkedin_url(claim_rows: list[ClaimRow]) -> str:
    """A concrete linkedin.com/in/ URL already present in the claims (PDL returns
    one; verified mentions sometimes do). Reading a KNOWN profile beats a blind
    name search — this is the seed for that. Returns '' when none is on hand."""
    for c in claim_rows:
        for field in (c.source_url or "", c.value or ""):
            m = _LINKEDIN_IN_RE.search(field)
            if m:
                return m.group(0).rstrip("/")
    return ""


def _resolve_linkedin_seed(
    http: httpx.Client,
    perplexity_key: str | None,
    person: Person,
    claim_rows: list[ClaimRow],
    *,
    verified_employer: str = "",
) -> tuple[str, ClaimRow | None]:
    """The human move: search "name + university + employer" and read the
    LinkedIn URL off the results, then reconcile it against PDL's guess.

    PDL returns a linkedin_url but it is sometimes a wrong slug (it gave
    `paul-marc-schweitzer`; the real profile is `pmschweitzer`). A plain
    Perplexity search surfaces the correct one and catches that error, while
    safely deferring to PDL for namesake-prone common names (the chooser holds
    PDL on weak/ambiguous hits). Returns the chosen seed URL plus, when the
    search CORRECTED PDL's guess, a claim recording the corrected URL so the
    right profile is persisted even if the downstream read fails. Never raises —
    a search outage degrades to PDL's URL alone."""
    pdl_url = _candidate_linkedin_url(claim_rows)
    employer = verified_employer or person.company or ""
    try:
        candidates = search_linkedin_candidates(
            http, perplexity_key, person.full_name,
            school=person.school or "", employer=employer,
        )
    except Exception:
        candidates = []
    chosen, reason = choose_linkedin_url(pdl_url, candidates)
    if not chosen:
        return pdl_url, None
    corrected: ClaimRow | None = None
    # Record the chosen URL only when search CHANGED PDL's guess (or PDL had
    # none) — an unchanged confirmation is already on file as PDL's own claim.
    if "overrides" in reason or "best search" in reason or (
        not pdl_url and chosen
    ):
        corrected = ClaimRow(
            claim_type="linkedin_url",
            value=chosen,
            source_url=chosen,
            quote=f"search-resolved ({reason})",
            confidence=0.7,
            extraction_method="linkedin_search",
        )
    return chosen, corrected


def _linkedin_pass(
    conn: sqlite3.Connection,
    firecrawl: Firecrawl,
    anthropic: Anthropic,
    person: Person,
    *,
    employer_hint: str,
    claim_rows: list[ClaimRow],
    trusted_count: int,
    li_budget: LinkedInBudget,
    fc_budget: FirecrawlBudget,
    policy: ResearchPolicy,
    role_start: int | None,
    seed_url: str = "",
    seed_overrides_gate: bool = True,
) -> _LinkedInPass:
    """Gated LinkedIn agent fetch + the fail-closed roster verifier.

    Every agent result is judged against the roster anchors before ANY of its
    claims join the pool (closing the old hole where agent output was extended
    in unverified), and the verdict is persisted to identity_candidates.
    Under REFRESH the gap-gate and min-source criteria are bypassed — the
    verifier IS the namesake protection — but both budgets still bind.

    `seed_url` is a known linkedin.com/in/ URL (from PDL or a verified mention):
    when present the agent READS that exact profile instead of blind-searching,
    which is far more reliable.

    `seed_overrides_gate` controls whether holding a seed FORCES the read. The
    head-to-head probe (2026-06-11) settled this: a seeded read lands only ~42%
    of the time and, where PDL is already rich, ADDS LESS than PDL (Will: PDL 11
    roles, LinkedIn 4 truncated) — so reading every seeded profile is wasteful.
    Where PDL is THIN, the read is the whole résumé (Payal 0→16, Bart 0→10). So
    the post-PDL caller passes False: the gap-gate (evaluated on the post-PDL
    claim set) fires the read only when the profile is still thin, while STILL
    reading the corrected seed URL when it does fire. The budgets and verifier
    bind under every setting."""
    if seed_url and seed_overrides_gate:
        decision = (
            LinkedInDecision(True, "seeded url")
            if li_budget.remaining > 0
            else LinkedInDecision(False, "batch LinkedIn budget spent")
        )
    elif bypass_linkedin_gap_gate(policy):
        decision = (
            LinkedInDecision(True, "refresh policy")
            if li_budget.remaining > 0
            else LinkedInDecision(False, "batch LinkedIn budget spent")
        )
    else:
        decision = li_budget.decide(
            claim_rows, trusted_count,
            grad_year=grad_year_from_class(person.school, person.titan_class),
            current_role_start_year=role_start,
        )
    if not fc_budget.decide().fire:
        print("  Firecrawl LinkedIn: skipped (deep-path budget spent)")
        return _LI_NOT_ATTEMPTED
    if not decision.fire:
        print(f"  Firecrawl LinkedIn: skipped ({decision.reason})")
        return _LI_NOT_ATTEMPTED

    try:
        li = fetch_linkedin(
            firecrawl, person.full_name, employer=employer_hint, city=person.city,
            profile_url=seed_url,
        )
    except PaymentRequiredError:
        print("  Firecrawl LinkedIn: no credits — skipped")
        return _LI_NOT_ATTEMPTED
    li_budget.charge(li.credits_used)
    fc_budget.charge(li.credits_used)
    if not li.found or not li.claim_rows:
        print(f"  Firecrawl LinkedIn: not found ({li.credits_used} credits)")
        return _LinkedInPass((), "", li.credits_used, 0, 0, True)

    url = next((c.source_url for c in li.claim_rows if c.source_url), "")
    verdict, vin, vout = verify_linkedin_profile(
        anthropic, person.full_name,
        profile_url=url,
        school=person.school,
        grad_year=grad_year_from_class(person.school, person.titan_class),
        roster_employer=person.company,
        city=person.city,
        claims=list(li.claim_rows),
    )
    upsert_candidate(conn, person.id, CandidateRow(
        source_url=url or f"linkedin-agent:{person.full_name}",
        confidence=verdict.confidence,
        decision=verdict.decision,
        reason=verdict.reason or "linkedin-agent profile verdict",
        model=HAIKU_MODEL,
    ))
    if not verdict.verified:
        print(f"  Firecrawl LinkedIn: {verdict.decision} — {verdict.reason} "
              f"({li.credits_used} credits)")
        return _LinkedInPass((), "", li.credits_used, vin, vout, True)

    employer = next(
        (c.value for c in li.claim_rows if c.claim_type == "current_employer"), ""
    )
    print(f"  Firecrawl LinkedIn: verified ({verdict.confidence:.2f}) — "
          f"{len(li.claim_rows)} claims ({li.credits_used} credits)")
    return _LinkedInPass(li.claim_rows, employer, li.credits_used, vin, vout, True)


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
    fc_budget: FirecrawlBudget | None = None,
    policy: ResearchPolicy = ResearchPolicy.BULK,
) -> _PersonUsage:
    """Run the full pipeline for one person and persist every stage. Records
    batch_status per phase so a crash mid-batch resumes cleanly. Returns the
    person's usage so the caller can fold it into the run-level cost log."""
    # A standalone call (no batch) gets a fresh single-person agent budget so the
    # LinkedIn gate still works; run() passes a shared budget across the batch.
    if li_budget is None:
        li_budget = LinkedInBudget(agent_batch_budget(1))
    if fc_budget is None:
        fc_budget = FirecrawlBudget(0)  # standalone call: no deep-path budget by default
    # Baseline discovery is Firecrawl-FREE: Perplexity finds candidate career URLs,
    # the free Jina reader fetches them, and we get the SAME Source objects the
    # identity gate + structuring consume. Firecrawl is reserved for the high-signal
    # deep top-up below, so a full run no longer burns ~15 credits/person here — and
    # baseline keeps working even at zero Firecrawl credits.
    disc = discover_via_jina(
        http, perplexity_key, person.full_name, person.company, person.city
    )
    base_source_rows = _source_rows(disc)
    replace_sources(conn, person.id, base_source_rows)
    anchors = _anchors(person)

    # Deterministic pre-filter first: slam-dunk multi-anchor matches are accepted
    # without Sonnet; only the ambiguous remainder is sent to the (billed) gate.
    pre = prefilter(anchors, disc.sources)
    identity = resolve_identity(anthropic, anchors, pre.ambiguous)
    verdicts = pre.decided + identity.verdicts
    base_candidate_rows = _candidate_rows(pre.decided, "prefilter") + _candidate_rows(
        identity.verdicts, "sonnet"
    )
    replace_candidates(conn, person.id, base_candidate_rows)
    mark_phase(conn, person.id, PHASE_IDENTITY, "done")

    trusted: tuple[Source, ...] = accepted_sources(disc.sources, verdicts)
    struct = structure_profile(anthropic, person.full_name, trusted)
    claim_rows = _claim_rows(struct)

    # What Claude actually verified as the current role — used to anchor the PDL
    # identity gate and to build sharper news/mention queries below.
    _verified_employer = (struct.profile.get("current_employer") or {}).get("value", "")
    _verified_title = (struct.profile.get("current_title") or {}).get("value", "")

    # === LINKEDIN-FIRST PASS (verified agent, before PDL) ===
    # When the policy or the PDL-independent signal warrants the deep path, the
    # LinkedIn agent runs FIRST: its verified current employer is the best
    # possible PDL anchor (the roster company is decades stale for older
    # classes; the web-verified employer is missing for exactly the thin
    # profiles that need help). A profile must clear the fail-closed roster
    # verifier before any of its claims are used.
    # ONLY the explicit LinkedIn-first policies (DEEP/REFRESH) reorder to run the
    # agent before PDL. Under BULK the agent stays in its original post-PDL
    # position (the deep-block fallback below) so BULK is behavior-identical to
    # the pre-rewrite pipeline EXCEPT that its LinkedIn output now passes through
    # the verifier — a pure safety add, never a reordering or a gating change.
    li_pass = _LI_NOT_ATTEMPTED
    # LinkedIn-agent accounting accumulates across BOTH possible firings (the
    # pre-PDL pass and the post-PDL seeded retry), so neither credits nor verify
    # tokens are lost when both run.
    li_credits_acc = li_vin_acc = li_vout_acc = n_li_acc = 0
    pre_deep = force_deep_path(policy)
    if pre_deep:
        li_pass = _linkedin_pass(
            conn, firecrawl, anthropic, person,
            employer_hint=_verified_employer or person.company,
            claim_rows=claim_rows,
            trusted_count=len(trusted),
            li_budget=li_budget,
            fc_budget=fc_budget,
            policy=policy,
            role_start=_current_role_start_year_from_claims(claim_rows),
            seed_url=_candidate_linkedin_url(claim_rows),
        )
        li_credits_acc += li_pass.credits
        li_vin_acc += li_pass.verify_in
        li_vout_acc += li_pass.verify_out
        n_li_acc += len(li_pass.claim_rows)
        if li_pass.claim_rows:
            claim_rows.extend(li_pass.claim_rows)

    # PDL deepens the verified résumé (canonical claim_types, identity-gated on
    # likelihood). Skips cleanly when its key is unset and never raises, so a
    # missing key or an outage degrades enrichment instead of aborting it.
    #
    # Anchor preference: the LinkedIn-verified current employer (freshest),
    # then the web-verified one, then the roster company — for an older class
    # the roster company is ~15-20 years stale and matches PDL's current
    # record poorly (the likely cause of low match rate).
    pdl_company = li_pass.verified_employer or _verified_employer or person.company
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
    pdl_usd_total = pdl.cost_usd if pdl else 0.0  # accumulates across the warm retry
    if pdl is not None and pdl.claim_rows:
        n_before = len(pdl.claim_rows)
        kept_pdl, pdl_pv_in, pdl_pv_out = verify_pdl_claims(
            anthropic, person.full_name,
            _verified_employer or person.company, person.city,
            list(pdl.claim_rows),
        )
        pdl_dropped = n_before - len(kept_pdl)
        claim_rows.extend(kept_pdl)

    # Resolve the LinkedIn URL the human way — search "name + school + employer"
    # and reconcile against PDL's guess — for EVERY person, BEFORE the Firecrawl
    # gate. This is a cheap Perplexity call (off the Firecrawl budget), so it
    # runs even on a zero-credit base sweep: the corrected URL is found, recorded
    # as a claim, and stashed as the seed for any later (deferred) deep read. The
    # actual Firecrawl page-read stays inside the deep block below.
    li_seed_url, li_seed_claim = _resolve_linkedin_seed(
        http, perplexity_key, person, claim_rows,
        verified_employer=li_pass.verified_employer or _verified_employer,
    )
    if li_seed_claim is not None:
        claim_rows.append(li_seed_claim)
        print(f"  LinkedIn search: corrected seed → {li_seed_url}")

    # === DEEP FIRECRAWL TOP-UP (high-signal only, under the run credit ceiling) ===
    # Baseline ran on free Jina. Firecrawl's billed calls are reserved for people who
    # warrant them: a confident PDL match, or real verified web presence. A thin,
    # no-footprint alum stops at the free baseline — Firecrawl can't surface what
    # isn't there, so we don't spend it there. Inside the gate, three Firecrawl uses:
    #   1. richer career pages (search+scrape) -> identity -> a 2nd structuring pass
    #      (the reconciler below merges its claims with the baseline ones),
    #   2. the LinkedIn agent (its own gap-filling triple-gate; biggest line item),
    #   3. the press/news pass.
    pdl_matched = bool(pdl and pdl.matched)
    # Policy override (DEEP/REFRESH): a targeted re-research pass spends on
    # exactly the no-footprint people the signal gate would skip — the operator
    # has already decided they warrant it (still bounded by fc/li budgets).
    deep = force_deep_path(policy) or is_high_signal(
        pdl_matched=pdl_matched,
        trusted_count=len(trusted),
        has_current_employer=bool(_verified_employer),
    )
    li_credits = n_li = 0
    career_credits = 0
    fc_news_credits = n_fc_news = 0
    fc_news_in = fc_news_out = 0
    deep_sonnet_in = deep_sonnet_out = deep_struct_in = deep_struct_out = 0
    if not deep:
        print("  Deep Firecrawl: skipped (low signal — free baseline only)")
    elif not fc_budget.decide().fire:
        print(f"  Deep Firecrawl: skipped ({fc_budget.decide().reason})")
    else:
        # 1. Richer career pages via Firecrawl, identity-gated and structured again.
        try:
            ddisc = discover(firecrawl, person.full_name, person.company, person.city)
        except PaymentRequiredError:
            ddisc = DiscoveryResult(
                full_name=person.full_name, sources=(), queries=(), credits_spent=0
            )
        fc_budget.charge(ddisc.credits_spent)
        career_credits = ddisc.credits_spent
        # Only process URLs the free baseline did NOT already cover — Perplexity and
        # Firecrawl often return the same page, and re-using it would (a) violate the
        # person_sources (person_id, url) UNIQUE key on the union persist, and (b)
        # waste a Sonnet identity call + Haiku structuring re-extracting the same text.
        _base_urls = {s.url for s in disc.sources}
        new_deep = tuple(s for s in ddisc.sources if s.url not in _base_urls)
        if new_deep:
            dpre = prefilter(anchors, new_deep)
            dident = resolve_identity(anthropic, anchors, dpre.ambiguous)
            dverdicts = dpre.decided + dident.verdicts
            dtrusted = accepted_sources(new_deep, dverdicts)
            dstruct = structure_profile(anthropic, person.full_name, dtrusted)
            claim_rows.extend(_claim_rows(dstruct))
            deep_sonnet_in, deep_sonnet_out = dident.input_tokens, dident.output_tokens
            deep_struct_in, deep_struct_out = dstruct.input_tokens, dstruct.output_tokens
            # Record deep provenance alongside the baseline (union of disjoint URLs).
            deep_source_rows = [
                SourceRow(url=s.url, domain=_domain(s.url), title=s.title, relevance=s.relevance)
                for s in new_deep
            ]
            replace_sources(conn, person.id, base_source_rows + deep_source_rows)
            replace_candidates(
                conn, person.id,
                base_candidate_rows
                + _candidate_rows(dpre.decided, "prefilter")
                + _candidate_rows(dident.verdicts, "sonnet"),
            )

        # 2. LinkedIn agent — fire when the pre-PDL pass didn't attempt it (BULK),
        #    OR it attempted but couldn't verify AND we now hold a concrete profile
        #    URL (usually surfaced by PDL): read THAT exact profile. A known-URL
        #    read is far more reliable than the blind name search that just missed
        #    — and even on a PDL-complete profile it corroborates the résumé. Same
        #    verified path; charges both ceilings inside the helper. The seed URL
        #    was already resolved + recorded above (base flow), so the read just
        #    consumes it — no second Perplexity search here.
        if (not li_pass.attempted) or (not li_pass.verified_employer and li_seed_url):
            _li_role_start = (
                pdl.attributes.current_role_start_year
                if pdl is not None
                else _current_role_start_year_from_claims(claim_rows)
            )
            li_pass = _linkedin_pass(
                conn, firecrawl, anthropic, person,
                employer_hint=_verified_employer or person.company,
                claim_rows=claim_rows,
                trusted_count=len(trusted),
                li_budget=li_budget,
                fc_budget=fc_budget,
                policy=policy,
                role_start=_li_role_start,
                seed_url=li_seed_url,
                # Targeted read: the corrected URL is read ONLY when the
                # post-PDL profile is still thin (gap-gate decides). REFRESH
                # still bypasses the gate inside the helper; under BULK/DEEP a
                # rich PDL profile (e.g. Will Carpenter) is left to PDL and the
                # ~189-credit read is spent only where PDL whiffed.
                seed_overrides_gate=False,
            )
            li_credits_acc += li_pass.credits
            li_vin_acc += li_pass.verify_in
            li_vout_acc += li_pass.verify_out
            n_li_acc += len(li_pass.claim_rows)
            if li_pass.claim_rows:
                claim_rows.extend(li_pass.claim_rows)

        # 2b. PDL warm-anchor retry: the first PDL attempt missed, but a VERIFIED
        #     LinkedIn employer has since arrived and differs from the anchor we
        #     used — one more try. A PDL miss is free (404s don't bill), so the
        #     retry only costs money when it succeeds, which is the point.
        if (
            pdl is not None and not pdl.matched
            and li_pass.verified_employer
            and li_pass.verified_employer != pdl_company
            and pdl_key
        ):
            print(f"  PDL warm retry: anchor '{li_pass.verified_employer}'")
            try:
                pdl_retry = enrich_pdl(
                    http, pdl_key, person.full_name,
                    li_pass.verified_employer, person.city,
                    school=person.school,
                    cost_usd_per_match=PDL_USD_PER_MATCH,
                )
            except PdlUnavailable as exc:
                # Quota exhausted ON THE RETRY: keep the first miss and the
                # already-collected (verified LinkedIn + web) claims rather than
                # letting the batch handler roll this person back. The retry is a
                # best-effort top-up, never load-bearing.
                print(f"  PDL warm retry: quota exhausted, skipped ({exc})")
                pdl_retry = None
            if pdl_retry is not None:
                pdl = pdl_retry
                pdl_usd_total += pdl.cost_usd
                if pdl.claim_rows:
                    kept_pdl, _rin, _rout = verify_pdl_claims(
                        anthropic, person.full_name,
                        li_pass.verified_employer, person.city,
                        list(pdl.claim_rows),
                    )
                    pdl_pv_in += _rin
                    pdl_pv_out += _rout
                    claim_rows.extend(kept_pdl)
            pdl_matched = bool(pdl.matched)

        # 3. Firecrawl + Claude press/news pass (verified employer/title queries).
        if fc_budget.decide().fire:
            try:
                news_disc = discover_news(
                    firecrawl, person.full_name, person.company,
                    verified_employer=_verified_employer,
                    verified_title=_verified_title,
                )
            except PaymentRequiredError:
                news_disc = NewsDiscoveryResult(sources=(), credits_spent=0)
            fc_budget.charge(news_disc.credits_spent)
            fc_news_credits = news_disc.credits_spent
            fc_news = extract_news_mentions(
                anthropic, person.full_name,
                _verified_employer or person.company, news_disc,
            )
            if fc_news.claim_rows:
                claim_rows.extend(fc_news.claim_rows)
            fc_news_in, fc_news_out = fc_news.input_tokens, fc_news.output_tokens
            n_fc_news = len(fc_news.claim_rows)

    # LinkedIn accounting covers BOTH call sites (pre-PDL and the deep/seeded
    # fallback). The helper can now fire twice (a blind miss then a URL-seeded
    # read), so credits and claim counts come from the accumulators, not the last
    # li_pass alone.
    li_credits = li_credits_acc
    n_li = n_li_acc

    # Compose a short_bio from the verified facts when no source handed us a
    # ready-made narrative. Built from the FULL résumé set (baseline + PDL + any deep
    # claims), so a PDL-matched person with zero scraped pages still gets a description.
    # Composes only from structured facts (never new knowledge); tagged synthesis.
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

    # Perplexity Sonar press pass: web-grounded, cited, person-specific press
    # (interviews, podcasts, awards/rankings, appointments). Off the Firecrawl
    # budget; gated by Sonar's own is_about_this_person reasoning, then fed — as
    # news_mention claims — through the SAME strict curator below as every other
    # press source. Key-gated and never-raises, like the /search pass.
    #
    # Adapt the queries to who the person ACTUALLY is — their verified role + PDL
    # industry (no hardcoded "finance"), and run targeted asks against their real
    # past firms (career-spanning search) so a former-employer story surfaces too.
    _industry = pdl.attributes.current_industry if pdl is not None else ""
    _past_companies = tuple(
        dict.fromkeys(  # de-dupe, preserve order
            cl.company_name
            for cl in (pdl.career_links if pdl is not None else ())
            if cl.company_name and not cl.is_current
        )
    )
    sonar = discover_press_sonar(
        http, person.full_name,
        _verified_employer or person.company, person.city,
        perplexity_key=perplexity_key,
        role=_verified_title,
        industry=_industry,
        past_companies=_past_companies,
    )
    if sonar.claim_rows:
        claim_rows.extend(sonar.claim_rows)

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
            # PDL industry is the primary sector signal (authoritative); the
            # classifier falls back to employer-name keywords. First employers
            # have no industry on record, so they classify by name alone. The
            # ambiguous catch-all remainder is upgraded later by the Haiku pass
            # in reclassify_sectors.py.
            current_sector=classify_sector(current_employer_val, pdl_attrs.current_industry),
            first_sector=classify_sector(first_employer),
            left_texas=left_texas(current_location_val),
            # Firm domain from PDL (free) → join key for the cached company layer.
            employer_domain=_company_domain(pdl_attrs.company_website),
            model=KPI_METHOD,
        ),
    )

    # Career-history firm links (current + past, with domain/title/years) → powers
    # the company page's "who works here now / who worked here before" view. Free
    # on a PDL match (domains ride the experience[] array). Replace-per-person.
    if pdl is not None and pdl.career_links:
        replace_person_companies(conn, person.id, [
            PersonCompany(
                person_id=person.id,
                domain=_company_domain(cl.domain),
                company_name=cl.company_name,
                title=cl.title,
                start_year=cl.start_year,
                end_year=cl.end_year,
                is_current=cl.is_current,
                source="pdl",
            )
            for cl in pdl.career_links
        ])

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
        anthropic, person.full_name, _verified_employer or person.company, clean_rows,
        # Verify would-be-shown items against the real article using the FREE Jina
        # reader (not billed Firecrawl) so an award page that only name-drops the
        # person is dropped, not shown as theirs — at zero credit cost.
        fetch_article=fetch_article_jina,
        # Past firms as context so the verifier correctly judges a former-employer
        # story as still about this person (not a namesake / not firm news).
        career=_past_companies,
    )
    replace_curated_news(conn, person.id, curated)

    mark_phase(conn, person.id, PHASE_STRUCTURING, "done")

    n_accept = sum(1 for v in verdicts if v.decision == DECISION_ACCEPT)
    n_review = sum(1 for v in verdicts if v.decision == DECISION_REVIEW)
    n_pre = len(pre.decided)
    n_pdl_claims = (len(pdl.claim_rows) - pdl_dropped) if pdl else 0
    n_mentions = len(mentions.claim_rows)
    n_sonar = len(sonar.claim_rows)
    print(
        f"  {person.full_name}: {len(disc.sources)} baseline sources -> "
        f"{n_accept} accepted ({n_pre} by pre-filter), {n_review} to review; "
        f"{len(claim_rows)} claims{' (+synth bio)' if bio else ''}"
        f"{f' (+{n_pdl_claims} PDL)' if n_pdl_claims else ''}"
        f"{f' (-{pdl_dropped} PDL gated)' if pdl_dropped else ''}"
        f"{' [deep]' if deep else ''}"
        f"{f' (+{n_li} LinkedIn)' if n_li else ''}"
        f"{f' (+{n_fc_news} press)' if n_fc_news else ''}"
        f"{f' (+{n_sonar} sonar press)' if n_sonar else ''}"
        f"{f' (+{n_mentions} verified mentions)' if n_mentions else ''}; "
        f"{len(pre.ambiguous)} sent to Sonnet; "
        f"{career_credits + li_credits + fc_news_credits} Firecrawl credits"
    )
    return _PersonUsage(
        credits=career_credits + li_credits,
        haiku_in=(
            struct.input_tokens
            + deep_struct_in
            + (bio.input_tokens if bio else 0)
            + fc_news_in
            + rec_in
            + pdl_pv_in
            + li_vin_acc
            + kpi_in
            + news_in
        ),
        haiku_out=(
            struct.output_tokens
            + deep_struct_out
            + (bio.output_tokens if bio else 0)
            + fc_news_out
            + rec_out
            + pdl_pv_out
            + li_vout_acc
            + kpi_out
            + news_out
        ),
        sonnet_in=identity.input_tokens + deep_sonnet_in,
        sonnet_out=identity.output_tokens + deep_sonnet_out,
        pdl_matches=1 if pdl_matched else 0,
        pdl_usd=pdl_usd_total,
        fc_news_credits=fc_news_credits,
        fc_news_articles=n_fc_news,
        perplexity_requests=mentions.perplexity_requests,
        sonar_requests=sonar.requests,
        sonar_usd=sonar.cost_usd,
    )


def run(
    limit: int,
    name: str | None,
    titan_class: int | None = None,
    school: str | None = None,
    max_credits: int | None = None,
    ids: list[int] | None = None,
    no_pdl: bool = False,
    policy: ResearchPolicy = ResearchPolicy.BULK,
    needs_deep: bool = False,
) -> int:
    # The deep pass targets base-sweep-flagged people and re-researches them
    # aggressively — force REFRESH so the LinkedIn read fires on the corrected
    # URL even for complete-looking profiles (the whole point of the second pass).
    if needs_deep:
        policy = ResearchPolicy.REFRESH
    firecrawl = Firecrawl(api_key=require_key("FIRECRAWL_API_KEY"))
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))

    # PDL / Perplexity keys are SOFT: their absence simply skips that source so
    # existing runs keep working before the keys are funded. Both are billed
    # (PDL per match, Perplexity per search), so we read them once per run.
    # --no-pdl hard-disables PDL for the run regardless of the key — used when the
    # monthly match quota must be preserved for hand-picked fills.
    pdl_key = None if no_pdl else os.getenv("PDL_API_KEY")
    perplexity_key = os.getenv("PERPLEXITY_API_KEY")

    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_enrichment_schema(conn)
        init_person_insights_schema(conn)
        init_person_company_schema(conn)
        init_news_schema(conn)
        people = _load_targets(conn, limit, name, titan_class, school, ids, needs_deep)
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
        # Hard run-level ceiling on the DEEP Firecrawl path (richer career scrape +
        # news pass + LinkedIn). Default to the live remaining balance so we never
        # plan to spend credits we don't have; --max-credits lets the operator cap it
        # lower. Baseline discovery is Firecrawl-free, so this bounds the ONLY
        # Firecrawl spend in the run.
        fc_ceiling = credits_before if max_credits is None else min(max_credits, credits_before)
        fc_budget = FirecrawlBudget(fc_ceiling)
        print(f"Firecrawl deep-path budget for this run: {fc_budget.remaining} credits")
        est_credits = 0
        haiku_in = haiku_out = sonnet_in = sonnet_out = 0
        pdl_matches = 0
        fc_news_credits = fc_news_articles = 0
        perplexity_requests = 0
        sonar_requests = 0
        sonar_usd = 0.0
        processed = 0
        pdl_exhausted = False

        with httpx.Client(timeout=30.0) as http:
            for person in people:
                print(f"\n=== {person.full_name} | {person.company} | {person.city} ===")
                try:
                    usage = enrich_person(
                        conn, firecrawl, anthropic, person, http, pdl_key, perplexity_key,
                        li_budget=li_budget, fc_budget=fc_budget, policy=policy,
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
                    sonar_requests += usage.sonar_requests
                    sonar_usd += usage.sonar_usd
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
                except PdlUnavailable as exc:
                    # PDL monthly quota is spent and won't recover this cycle. Per the
                    # operator's call: do NOT enrich the rest on a degraded (PDL-less)
                    # path — leave them un-enriched (rolled back → still pending) for a
                    # future run when PDL renews. Stop the batch cleanly here.
                    conn.rollback()
                    print(
                        f"\n\nPDL QUOTA EXHAUSTED ({exc}) — stopping cleanly.\n"
                        f"{person.full_name} and all remaining people are left "
                        "un-enriched (still pending) for a future run when PDL renews.",
                        file=sys.stderr,
                    )
                    pdl_exhausted = True
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
    if ids:
        batch_label = f"deep-pass-{processed}"
    else:
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
        sonar_requests=sonar_requests,
        sonar_usd=sonar_usd,
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
    # Exit 3 signals PDL-quota stop so a multi-cohort runner can break the sequence
    # instead of re-hitting the exhausted quota on every following cohort.
    return 3 if pdl_exhausted else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phase2-enrich", description=__doc__)
    p.add_argument("--limit", type=int, default=5, help="How many un-enriched alumni to process")
    p.add_argument("--name", default=None, help="Enrich one specific person by full name")
    p.add_argument("--class", dest="titan_class", type=int, default=None,
                   help="Target an un-enriched cohort by Titan class number")
    p.add_argument("--school", default=None,
                   help="Restrict --class to one school (e.g. 'Texas A&M')")
    p.add_argument("--max-credits", dest="max_credits", type=int, default=None,
                   help="Hard ceiling on DEEP Firecrawl credits for this run "
                        "(baseline discovery is free); defaults to the live balance")
    p.add_argument("--ids", default=None,
                   help="Comma-separated person IDs to (re-)enrich, e.g. '770,817'. "
                        "Bypasses the done-check: targets are rebuilt in place")
    p.add_argument("--no-pdl", dest="no_pdl", action="store_true",
                   help="Hard-disable PDL for this run (preserve the monthly quota)")
    p.add_argument("--policy", default=None,
                   choices=[pol.value for pol in ResearchPolicy],
                   help="Research policy: bulk (all gates), deep (force the deep "
                        "Firecrawl path), refresh (deep + LinkedIn agent fires even "
                        "for complete-looking profiles)")
    p.add_argument("--force-deep", dest="force_deep", action="store_true",
                   help="DEPRECATED alias for --policy deep")
    p.add_argument("--needs-deep", dest="needs_deep", action="store_true",
                   help="Deep pass: target people the base sweep flagged "
                        "(person_insights.needs_deep_search=1). Forces --policy "
                        "refresh and bypasses the done-check")
    return p


def _parse_ids(raw: str | None) -> list[int] | None:
    if raw is None:
        return None
    try:
        ids = [int(tok) for tok in raw.split(",") if tok.strip()]
    except ValueError as exc:
        raise SystemExit(f"--ids must be comma-separated integers: {exc}")
    if not ids:
        raise SystemExit("--ids given but no valid IDs parsed")
    return ids


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.policy is not None:
        policy = ResearchPolicy.parse(args.policy)
    elif args.force_deep:
        print("--force-deep is deprecated; use --policy deep", file=sys.stderr)
        policy = ResearchPolicy.DEEP
    else:
        policy = ResearchPolicy.BULK
    return run(limit=args.limit, name=args.name,
               titan_class=args.titan_class, school=args.school,
               max_credits=args.max_credits, ids=_parse_ids(args.ids),
               no_pdl=args.no_pdl, policy=policy, needs_deep=args.needs_deep)


if __name__ == "__main__":
    sys.exit(main())
