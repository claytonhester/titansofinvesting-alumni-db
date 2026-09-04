"""Auth failures must be loud: every never-raises adapter lets a rejected key
(HTTP 401/403 -> config.AuthError) through, and the orchestrator aborts at once."""
from __future__ import annotations

import anthropic
import httpx
import pytest

import phase2_enrich
from company_enrich import enrich_company
from config import AuthError
from enrichment_store import ClaimRow
from linkedin_verify import verify_linkedin_profile
from pdl_enrich import enrich_pdl
from perplexity_enrich import fetch_perplexity
from sonar_news import discover_press_sonar
from tests.test_phase2_circuit_breaker import _patch_run_externals, _seed_db


def _client(status: int) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(status, json={"error": "nope"})))


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403])
def test_pdl_rejected_key_raises(status: int) -> None:
    with pytest.raises(AuthError, match="PDL"):
        enrich_pdl(_client(status), "bad", "Jane Doe", "Acme", "Austin",
                   cost_usd_per_match=0.28, attempts=1)


@pytest.mark.unit
def test_pdl_other_4xx_still_degrades() -> None:
    # Regression guard: only auth is loud; a bad request stays a quiet miss.
    res = enrich_pdl(_client(400), "k", "Jane Doe", "Acme", "Austin",
                     cost_usd_per_match=0.28, attempts=1)
    assert res.claim_rows == () and res.matched is False


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403])
def test_perplexity_rejected_key_raises(status: int) -> None:
    with pytest.raises(AuthError, match="Perplexity"):
        fetch_perplexity(_client(status), "bad", "Jane Doe", attempts=1)


@pytest.mark.unit
def test_perplexity_other_4xx_still_empty() -> None:
    assert fetch_perplexity(_client(400), "k", "Jane Doe", attempts=1) == []


@pytest.mark.unit
def test_sonar_rejected_key_raises() -> None:
    with pytest.raises(AuthError, match="Sonar"):
        discover_press_sonar(_client(401), "Jane Doe", "Acme", "Austin",
                             perplexity_key="bad", facets=("x",))


@pytest.mark.unit
def test_sonar_server_error_still_degrades() -> None:
    res = discover_press_sonar(_client(503), "Jane Doe", "Acme", "Austin",
                               perplexity_key="k", facets=("x",))
    assert res.claim_rows == () and res.requests == 1


@pytest.mark.unit
@pytest.mark.parametrize("status", [401, 403])
def test_company_enrich_rejected_key_raises(status: int) -> None:
    with pytest.raises(AuthError, match="PDL"):
        enrich_company(_client(status), "bad", "bcg.com", attempts=1)


@pytest.mark.unit
def test_company_enrich_other_4xx_is_transient_none() -> None:
    assert enrich_company(_client(400), "k", "bcg.com", attempts=1) is None


# --- the Claude verifier: fail-closed for everything EXCEPT a rejected key ------

class _RaisingClient:
    def __init__(self, exc):
        self._exc = exc
        self.messages = self

    def create(self, **_):
        raise self._exc


def _sdk_auth_error(cls):
    resp = httpx.Response(401, request=httpx.Request("POST", "https://api.anthropic.com"))
    return cls("invalid x-api-key", response=resp, body=None)


def _verify(client):
    return verify_linkedin_profile(
        client, "Jane Doe", profile_url="https://linkedin.com/in/x",
        school="Texas A&M", grad_year=2007, roster_employer="JP Morgan",
        city="Houston",
        claims=[ClaimRow("education", "BBA", "https://linkedin.com/in/x", "", 0.8, "li")],
    )


@pytest.mark.unit
@pytest.mark.parametrize("cls", [anthropic.AuthenticationError, anthropic.PermissionDeniedError])
def test_verifier_lets_sdk_auth_error_through(cls) -> None:
    with pytest.raises(AuthError, match="Anthropic"):
        _verify(_RaisingClient(_sdk_auth_error(cls)))


@pytest.mark.unit
def test_verifier_still_fail_closed_on_other_errors() -> None:
    verdict, tok_in, tok_out = _verify(_RaisingClient(RuntimeError("timeout")))
    assert verdict.decision == "rejected" and (tok_in, tok_out) == (0, 0)


# --- orchestrator: abort immediately, not after MAX_CONSECUTIVE_ERRORS ----------

@pytest.mark.parametrize("exc", [
    AuthError("PDL rejected the API key (HTTP 401)"),
    _sdk_auth_error(anthropic.AuthenticationError),
])
def test_run_aborts_on_first_auth_failure(tmp_path, monkeypatch, capsys, exc):
    db = str(tmp_path / "auth.db")
    _seed_db(db, n=8)
    calls = {"n": 0}

    def _rejected(*a, **k):
        calls["n"] += 1
        raise exc

    _patch_run_externals(monkeypatch, db, _rejected)
    rc = phase2_enrich.run(limit=8, name=None, rerun_enriched=True, max_credits=0)

    assert rc == 4
    assert calls["n"] == 1  # not MAX_CONSECUTIVE_ERRORS: a bad key never self-heals
    assert "AUTH FAILURE" in capsys.readouterr().err


def test_auth_failure_leaves_person_pending_not_errored(tmp_path, monkeypatch):
    import sqlite3
    db = str(tmp_path / "auth2.db")
    _seed_db(db, n=2)
    _patch_run_externals(monkeypatch, db, lambda *a, **k: (_ for _ in ()).throw(AuthError("x")))
    phase2_enrich.run(limit=2, name=None, rerun_enriched=True, max_credits=0)
    n_err = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM batch_status WHERE status='error'").fetchone()[0]
    assert n_err == 0  # the key was at fault, not the profile


def test_seed_resolver_does_not_swallow_auth_error(monkeypatch):
    def _boom(*a, **k):
        raise AuthError("Perplexity rejected the API key (HTTP 401)")
    monkeypatch.setattr(phase2_enrich, "search_linkedin_candidates", _boom)
    person = phase2_enrich.Person(1, "Jane Doe", "Acme", "Austin", "Texas A&M", 2)
    with pytest.raises(AuthError):
        phase2_enrich._resolve_linkedin_seed(object(), "key", person, [])
