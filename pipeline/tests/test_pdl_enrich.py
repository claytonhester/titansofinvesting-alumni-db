"""Unit tests for the People Data Labs adapter — all HTTP is mocked, no spend.

Covers the three things that keep PDL honest and cheap:
1. A confident match maps onto the canonical claim_types in resume.ts's shapes.
2. The likelihood gate: a 200 BELOW the floor is billed but woven in as nothing.
3. A 404 (no match at/above the gate) is free and yields no claims.
"""
from __future__ import annotations

import httpx
import pytest

from pdl_enrich import PDL_ACCEPT, enrich_pdl

_PER_MATCH = 0.28


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _full_record() -> dict:
    return {
        "likelihood": 9,
        "data": {
            "job_title": "Managing Director",
            "job_company_name": "Apex Capital",
            "location_name": "Austin, Texas",
            "linkedin_url": "linkedin.com/in/jane-doe",
            "job_company_industry": "investment management",
            "job_company_size": "1001-5000",
            "job_title_role": "finance",
            "job_title_levels": ["director", "partner"],
            "job_start_date": "2018-04",
            "inferred_years_experience": 14,
            "linkedin_connections": 832,
            "skills": ["Private Equity", "Valuation", "private equity", "LBO"],
            "certifications": ["CFA", {"name": "CPA"}],
            "experience": [
                {
                    "title": {"name": "Managing Director"},
                    "company": {"name": "Apex Capital"},
                    "start_date": "2018-04",
                    "end_date": None,
                    "is_primary": True,
                },
                {
                    "title": {"name": "Associate"},
                    "company": {"name": "Old Bank"},
                    "start_date": "2014",
                    "end_date": "2018",
                    "is_primary": False,
                },
            ],
            "education": [
                {"school": {"name": "Rice University"}, "degrees": ["MBA"]},
            ],
            "profiles": [
                {"network": "twitter", "url": "twitter.com/janedoe"},
                {"network": "linkedin", "url": "linkedin.com/in/jane-doe"},
            ],
        },
    }


@pytest.mark.unit
def test_confident_match_maps_canonical_claims() -> None:
    """A high-likelihood match becomes canonical claim_types the résumé parses."""
    client = _client(lambda req: httpx.Response(200, json=_full_record()))
    result = enrich_pdl(
        client, "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH,
    )

    assert result.matched is True
    assert result.likelihood == 9
    assert result.cost_usd == _PER_MATCH

    by_type: dict[str, list] = {}
    for row in result.claim_rows:
        by_type.setdefault(row.claim_type, []).append(row)
        assert row.extraction_method == "pdl"
        assert row.confidence == pytest.approx(0.9)

    assert by_type["current_title"][0].value == "Managing Director"
    assert by_type["current_employer"][0].value == "Apex Capital"
    assert by_type["location"][0].value == "Austin, Texas"
    assert len(by_type["career_history"]) == 2
    assert by_type["education"][0].value == "MBA from Rice University"
    # LinkedIn promoted to a full URL; the duplicate linkedin profile is dropped,
    # leaving the LinkedIn link plus the non-linkedin twitter profile.
    link_urls = {r.source_url for r in by_type["public_links"]}
    assert "https://linkedin.com/in/jane-doe" in link_urls
    assert "https://twitter.com/janedoe" in link_urls


@pytest.mark.unit
def test_extracts_skills_and_certifications_as_claims() -> None:
    client = _client(lambda req: httpx.Response(200, json=_full_record()))
    result = enrich_pdl(
        client, "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH,
    )
    skills = [r.value for r in result.claim_rows if r.claim_type == "skill"]
    certs = [r.value for r in result.claim_rows if r.claim_type == "certification"]
    # case-insensitive de-dup keeps "Private Equity", drops "private equity"
    assert skills == ["Private Equity", "Valuation", "LBO"]
    assert certs == ["CFA", "CPA"]  # string and {name:...} forms both handled


@pytest.mark.unit
def test_extracts_profile_attributes() -> None:
    client = _client(lambda req: httpx.Response(200, json=_full_record()))
    attrs = enrich_pdl(
        client, "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH,
    ).attributes
    assert attrs.current_industry == "investment management"
    assert attrs.current_company_size == "1001-5000"
    assert attrs.job_function == "finance"
    assert attrs.pdl_seniority == "director, partner"
    assert attrs.current_role_start_year == 2018
    assert attrs.years_experience == 14
    assert attrs.linkedin_connections == 832


@pytest.mark.unit
def test_attributes_default_empty_when_absent() -> None:
    record = {"likelihood": 9, "data": {"job_title": "Analyst"}}
    client = _client(lambda req: httpx.Response(200, json=record))
    attrs = enrich_pdl(
        client, "key", "Jane Doe", "", "", cost_usd_per_match=_PER_MATCH,
    ).attributes
    assert attrs.current_industry == "" and attrs.years_experience is None
    assert attrs.current_role_start_year is None


@pytest.mark.unit
def test_zero_years_experience_preserved_not_nulled() -> None:
    """A real 0 (founder <1yr, 0 connections) must survive, not become None."""
    record = {"likelihood": 9, "data": {
        "job_title": "Founder", "inferred_years_experience": 0,
        "linkedin_connections": 0,
    }}
    client = _client(lambda req: httpx.Response(200, json=record))
    attrs = enrich_pdl(
        client, "key", "Jane Doe", "", "", cost_usd_per_match=_PER_MATCH,
    ).attributes
    assert attrs.years_experience == 0
    assert attrs.linkedin_connections == 0


@pytest.mark.unit
def test_career_history_uses_parseable_quote_shape() -> None:
    """A dated role emits the 'YYYY - end Title @ Company' quote resume.ts parses
    first, so the timeline renders with its years."""
    client = _client(lambda req: httpx.Response(200, json=_full_record()))
    result = enrich_pdl(
        client, "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH,
    )
    history = [r for r in result.claim_rows if r.claim_type == "career_history"]
    old_bank = next(r for r in history if "Old Bank" in r.value)
    assert old_bank.quote == "2014 - 2018 Associate @ Old Bank"
    assert old_bank.value == "Associate at Old Bank (2014-2018)"


@pytest.mark.unit
def test_below_gate_match_is_billed_but_woven_in_as_nothing() -> None:
    """A 200 whose likelihood is under the floor was charged by PDL, so we record
    the cost — but trust none of its facts."""
    low = {"likelihood": PDL_ACCEPT - 1, "data": {"job_title": "Someone Else"}}
    client = _client(lambda req: httpx.Response(200, json=low))
    result = enrich_pdl(
        client, "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH,
    )
    assert result.matched is True
    assert result.cost_usd == _PER_MATCH
    assert result.claim_rows == ()


@pytest.mark.unit
def test_no_match_404_is_free_and_empty() -> None:
    """A 404 means no match at/above the gate — PDL charges nothing and we add
    no claims."""
    client = _client(lambda req: httpx.Response(404, json={"error": "no match"}))
    result = enrich_pdl(
        client, "key", "Nobody Here", "", "",
        cost_usd_per_match=_PER_MATCH,
    )
    assert result.matched is False
    assert result.cost_usd == 0.0
    assert result.claim_rows == ()


@pytest.mark.unit
def test_min_likelihood_passed_to_server() -> None:
    """The gate is enforced server-side too, so PDL returns 404 (free) below it."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(404)

    enrich_pdl(
        _client(handler), "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH,
    )
    assert seen["min_likelihood"] == str(PDL_ACCEPT)
    assert seen["company"] == "Apex Capital"
    assert seen["location"] == "Austin"


@pytest.mark.unit
def test_unknown_anchors_are_omitted_from_query() -> None:
    """'(unknown)' company/city are placeholders, not real anchors — don't send."""
    seen: dict[str, str] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(dict(req.url.params))
        return httpx.Response(404)

    enrich_pdl(
        _client(handler), "key", "Jane Doe", "(unknown)", "(unknown)",
        cost_usd_per_match=_PER_MATCH,
    )
    assert "company" not in seen
    assert "location" not in seen


@pytest.mark.unit
def test_empty_name_short_circuits_without_request() -> None:
    """No name means nothing to query — return empty without touching the network."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise AssertionError("should not issue a request for an empty name")

    result = enrich_pdl(
        _client(handler), "key", "   ", "Co", "City",
        cost_usd_per_match=_PER_MATCH,
    )
    assert result.claim_rows == ()
    assert result.cost_usd == 0.0


@pytest.mark.unit
def test_network_failure_degrades_to_empty() -> None:
    """A transport error must never raise — it degrades this person to no claims."""
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    result = enrich_pdl(
        _client(handler), "key", "Jane Doe", "Apex Capital", "Austin",
        cost_usd_per_match=_PER_MATCH, attempts=2, backoff_base=0.0,
    )
    assert result.matched is False
    assert result.claim_rows == ()
    assert result.cost_usd == 0.0
