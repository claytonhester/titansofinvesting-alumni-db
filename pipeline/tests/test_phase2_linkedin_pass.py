"""Tests for phase2's verified LinkedIn pass (_linkedin_pass) and policy gating."""
from __future__ import annotations

import sqlite3

import pytest

import phase2_enrich
from db import init_schema
from deep_gate import FirecrawlBudget
from enrichment_store import ClaimRow, init_enrichment_schema
from linkedin_firecrawl import LinkedInBudget, LinkedInResult
from linkedin_verify import LinkedInVerdict
from phase2_enrich import Person, _linkedin_pass
from research_policy import ResearchPolicy


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    conn.execute(
        "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, raw_entry) "
        "VALUES (1, 'Jane Doe', 'jane-doe', 2, 'Texas A&M', 'JP Morgan', "
        "'Houston', 'https://roster', 'raw')"
    )
    return conn


_PERSON = Person(id=1, full_name="Jane Doe", company="JP Morgan",
                 city="Houston", school="Texas A&M", titan_class=2)

_URL = "https://www.linkedin.com/in/jane-doe"


def _li_result(found=True, credits=42):
    claims = (
        ClaimRow("current_employer", "Acme Capital", _URL, "", 0.8, "firecrawl-linkedin"),
        ClaimRow("career_history", "Partner at Acme Capital (2018-present)", _URL,
                 "", 0.8, "firecrawl-linkedin"),
    ) if found else ()
    return LinkedInResult(claims, found, credits)


# A profile rich enough that the gap-gate says "complete": current role +
# education + 3 career entries with a tight grad->role-start window.
_COMPLETE_PROFILE = [
    ClaimRow("current_employer", "Acme", "", "", 0.9, "x"),
    ClaimRow("current_title", "CEO", "", "", 0.9, "x"),
    ClaimRow("education", "BBA from A&M", "", "", 0.9, "x"),
    ClaimRow("career_history", "CEO at Acme (2008-present)", "", "", 0.9, "x"),
    ClaimRow("career_history", "VP at Bank (2007-2008)", "", "", 0.9, "x"),
    ClaimRow("career_history", "Analyst at Bank (2006-2007)", "", "", 0.9, "x"),
]


def _call(monkeypatch, *, policy, claims, verdict="verified", li_budget=None,
          fc_budget=None, found=True):
    monkeypatch.setattr(phase2_enrich, "fetch_linkedin",
                        lambda *a, **k: _li_result(found=found))
    monkeypatch.setattr(
        phase2_enrich, "verify_linkedin_profile",
        lambda *a, **k: (LinkedInVerdict(verdict, "test", 0.9), 8, 3))
    conn = _conn()
    return _linkedin_pass(
        conn, object(), object(), _PERSON,
        employer_hint="JP Morgan",
        claim_rows=claims,
        trusted_count=2,
        li_budget=li_budget or LinkedInBudget(500),
        fc_budget=fc_budget or FirecrawlBudget(500),
        policy=policy,
        role_start=2008,
    ), conn


def test_verified_claims_join_the_pool(monkeypatch):
    result, conn = _call(monkeypatch, policy=ResearchPolicy.DEEP, claims=[])
    assert result.attempted and result.claim_rows
    assert result.verified_employer == "Acme Capital"
    row = conn.execute("SELECT decision FROM identity_candidates WHERE person_id=1").fetchone()
    assert row["decision"] == "verified"


def test_rejected_profile_contributes_no_claims_but_audits(monkeypatch):
    result, conn = _call(monkeypatch, policy=ResearchPolicy.DEEP, claims=[],
                         verdict="rejected")
    assert result.attempted and not result.claim_rows
    assert result.verified_employer == ""
    assert result.verify_in > 0  # the verifier DID run (regression: no more
    row = conn.execute("SELECT decision FROM identity_candidates").fetchone()
    assert row["decision"] == "rejected"  # unverified claim_rows.extend)


def test_bulk_gap_gate_skips_complete_profile(monkeypatch):
    result, _ = _call(monkeypatch, policy=ResearchPolicy.BULK,
                      claims=list(_COMPLETE_PROFILE))
    assert not result.attempted  # "profile already complete" under BULK


def test_refresh_bypasses_gap_gate_for_complete_profile(monkeypatch):
    result, _ = _call(monkeypatch, policy=ResearchPolicy.REFRESH,
                      claims=list(_COMPLETE_PROFILE))
    assert result.attempted and result.claim_rows  # the Bart Howe fix


def test_refresh_still_bound_by_linkedin_budget(monkeypatch):
    result, _ = _call(monkeypatch, policy=ResearchPolicy.REFRESH,
                      claims=[], li_budget=LinkedInBudget(0))
    assert not result.attempted


def test_any_policy_bound_by_firecrawl_budget(monkeypatch):
    result, _ = _call(monkeypatch, policy=ResearchPolicy.REFRESH,
                      claims=[], fc_budget=FirecrawlBudget(0))
    assert not result.attempted


def test_not_found_charges_budgets_without_verifier(monkeypatch):
    li_budget = LinkedInBudget(500)
    result, conn = _call(monkeypatch, policy=ResearchPolicy.DEEP, claims=[],
                         li_budget=li_budget, found=False)
    assert result.attempted and not result.claim_rows
    assert result.credits == 42 and li_budget.remaining == 458
    assert result.verify_in == 0  # verifier not called on a not-found
    assert conn.execute("SELECT COUNT(*) FROM identity_candidates").fetchone()[0] == 0
