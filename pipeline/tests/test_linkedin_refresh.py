"""Tests for the append-only LinkedIn refresh runner."""
from __future__ import annotations

import json
import sqlite3

import pytest

import linkedin_refresh
from db import init_schema
from enrichment_store import ClaimRow, init_enrichment_schema, replace_claims
from linkedin_firecrawl import LinkedInResult
from linkedin_verify import LinkedInVerdict
from person_insights_store import (
    PersonInsight,
    init_person_insights_schema,
    upsert_person_insight,
)


# --- fixtures -------------------------------------------------------------------

def _db(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    init_person_insights_schema(conn)
    conn.execute(
        "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, raw_entry) "
        "VALUES (1, 'Jane Doe', 'jane-doe', 2, 'Texas A&M', 'JP Morgan', "
        "'Houston', 'https://roster', 'raw')"
    )
    upsert_person_insight(conn, PersonInsight(
        person_id=1, grad_year=2007, grad_year_source="class-map",
        first_employer="JP Morgan", on_buy_side=False, reached_md=False,
        founder_partner=False, still_first_firm=False,
    ))
    replace_claims(conn, 1, [
        ClaimRow("current_employer", "Acme Capital", "https://acme.com", "", 0.9, "firecrawl"),
    ])
    conn.commit()
    return path, conn


def _li_claims():
    url = "https://www.linkedin.com/in/jane-doe"
    return (
        ClaimRow("current_title", "Partner", url, "", 0.8, "firecrawl-linkedin"),
        ClaimRow("current_employer", "Acme Capital", url, "", 0.8, "firecrawl-linkedin"),
        ClaimRow("career_history", "Partner at Acme Capital (2018-present)", url,
                 "2018 - present Partner @ Acme Capital", 0.8, "firecrawl-linkedin"),
        ClaimRow("public_links", "LinkedIn", url, "", 0.8, "firecrawl-linkedin"),
    )


class _FakeReconcileResp:
    class _U:
        input_tokens = 7
        output_tokens = 3

    def __init__(self):
        self.usage = self._U()
        # Unusable JSON -> reconcile keeps all claims verbatim (its safety net).
        self.content = [type("B", (), {"type": "text", "text": "{}"})()]


class _FakeAnthropic:
    def __init__(self):
        self.messages = self

    def create(self, **_):
        return _FakeReconcileResp()


def _person_row(conn):
    return conn.execute(
        "SELECT p.id, p.full_name, p.school, p.titan_class, p.initial_company, "
        "p.city, pi.grad_year, "
        "(SELECT c.value FROM claims c WHERE c.person_id = p.id "
        " AND c.claim_type = 'current_employer' LIMIT 1) AS current_employer "
        "FROM people p LEFT JOIN person_insights pi ON pi.person_id = p.id "
        "WHERE p.id = 1"
    ).fetchone()


# --- target selection -------------------------------------------------------------

def test_target_ids_from_cli_string():
    assert linkedin_refresh._target_ids("770, 16,840", "ignored") == [770, 16, 840]


def test_target_ids_rejects_garbage():
    with pytest.raises(SystemExit):
        linkedin_refresh._target_ids("770,abc", "ignored")


def test_target_ids_walks_nested_targets_file(tmp_path):
    f = tmp_path / "targets.json"
    f.write_text(json.dumps({
        "rerun_full_spine": {"tier1": [{"id": 770, "name": "A"}, {"id": 16, "name": "B"}]},
        "append_only": {"x": [{"id": 770}, {"id": 861}]},
    }))
    assert linkedin_refresh._target_ids(None, str(f)) == [770, 16, 861]  # deduped, ordered


def test_target_ids_missing_file():
    with pytest.raises(SystemExit):
        linkedin_refresh._target_ids(None, "/nope/missing.json")


# --- refresh_person ---------------------------------------------------------------

def test_verified_profile_appends_and_reconciles(tmp_path, monkeypatch):
    path, conn = _db(tmp_path)
    monkeypatch.setattr(linkedin_refresh, "fetch_linkedin",
                        lambda *a, **k: LinkedInResult(_li_claims(), True, 42))
    monkeypatch.setattr(linkedin_refresh, "verify_linkedin_profile",
                        lambda *a, **k: (LinkedInVerdict("verified", "era+employer", 0.93), 11, 4))

    credits, hin, hout, upgraded = linkedin_refresh.refresh_person(
        conn, object(), _FakeAnthropic(), _person_row(conn))
    assert credits == 42 and upgraded
    assert hin > 0 and hout > 0

    values = {r[0] for r in conn.execute("SELECT value FROM claims WHERE person_id=1")}
    assert any("2018" in v for v in values)          # LinkedIn career landed
    verdict = conn.execute(
        "SELECT decision, source_url FROM identity_candidates WHERE person_id=1").fetchone()
    assert verdict["decision"] == "verified"
    assert "linkedin.com/in/jane-doe" in verdict["source_url"]


def test_rejected_profile_writes_only_audit_row(tmp_path, monkeypatch):
    path, conn = _db(tmp_path)
    monkeypatch.setattr(linkedin_refresh, "fetch_linkedin",
                        lambda *a, **k: LinkedInResult(_li_claims(), True, 37))
    monkeypatch.setattr(linkedin_refresh, "verify_linkedin_profile",
                        lambda *a, **k: (LinkedInVerdict("rejected", "wrong era", 0.2), 9, 2))

    before = conn.execute("SELECT COUNT(*) FROM claims WHERE person_id=1").fetchone()[0]
    credits, _, _, upgraded = linkedin_refresh.refresh_person(
        conn, object(), _FakeAnthropic(), _person_row(conn))
    after = conn.execute("SELECT COUNT(*) FROM claims WHERE person_id=1").fetchone()[0]
    assert not upgraded and credits == 37
    assert before == after                                  # no claim writes
    verdict = conn.execute(
        "SELECT decision FROM identity_candidates WHERE person_id=1").fetchone()
    assert verdict["decision"] == "rejected"                 # audit trail persisted


def test_agent_not_found_writes_nothing(tmp_path, monkeypatch):
    path, conn = _db(tmp_path)
    monkeypatch.setattr(linkedin_refresh, "fetch_linkedin",
                        lambda *a, **k: LinkedInResult((), False, 18))
    credits, hin, hout, upgraded = linkedin_refresh.refresh_person(
        conn, object(), _FakeAnthropic(), _person_row(conn))
    assert (credits, hin, hout, upgraded) == (18, 0, 0, False)
    assert conn.execute("SELECT COUNT(*) FROM identity_candidates").fetchone()[0] == 0


# --- dry run ----------------------------------------------------------------------

def test_dry_run_makes_no_api_calls_and_no_writes(tmp_path, monkeypatch, capsys):
    path, conn = _db(tmp_path)
    conn.close()
    targets = tmp_path / "targets.json"
    targets.write_text(json.dumps({"people": [{"id": 1}]}))

    def _explode(*a, **k):
        raise AssertionError("dry run must not construct API clients")

    monkeypatch.setattr(linkedin_refresh, "Firecrawl", _explode)
    monkeypatch.setattr(linkedin_refresh, "Anthropic", _explode)

    rc = linkedin_refresh.main(["--targets-file", str(targets), "--db", str(path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "Jane Doe" in out

    check = sqlite3.connect(path)
    assert check.execute("SELECT COUNT(*) FROM claims").fetchone()[0] == 1  # untouched
