"""Unit tests for linkedin_firecrawl: prompt build, claim mapping, and the
fetch wrapper's success / no-match / out-of-credits behavior (with a fake client,
since the live agent call needs Firecrawl credits)."""
from __future__ import annotations

import pytest
from firecrawl.v2.utils.error_handler import PaymentRequiredError

from enrichment_store import ClaimRow
from linkedin_firecrawl import (
    EXTRACTION_METHOD,
    build_prompt,
    fetch_linkedin,
    map_claims,
    profile_needs_linkedin,
)


def _c(claim_type, value="x"):
    return ClaimRow(claim_type=claim_type, value=value, source_url="", quote="",
                    confidence=0.8, extraction_method="pdl")


def _rich_profile():
    """A profile complete enough to skip the billed LinkedIn agent."""
    return [
        _c("current_employer", "Acme"),
        _c("education", "BBA, Texas A&M"),
        _c("career_history", "Partner at Acme"),
        _c("career_history", "VP at Beta"),
        _c("career_history", "Analyst at Gamma"),
    ]


def test_profile_needs_linkedin_false_when_complete():
    assert profile_needs_linkedin(_rich_profile()) is False


def test_profile_needs_linkedin_true_missing_employer():
    claims = [c for c in _rich_profile() if c.claim_type != "current_employer"]
    assert profile_needs_linkedin(claims) is True


def test_profile_needs_linkedin_true_missing_education():
    claims = [c for c in _rich_profile() if c.claim_type != "education"]
    assert profile_needs_linkedin(claims) is True


def test_profile_needs_linkedin_true_too_few_roles():
    claims = [
        _c("current_employer", "Acme"),
        _c("education", "BBA"),
        _c("career_history", "Partner at Acme"),  # only 1 role < 3
    ]
    assert profile_needs_linkedin(claims) is True


def test_profile_needs_linkedin_true_on_empty():
    assert profile_needs_linkedin([]) is True


def test_build_prompt_includes_qualifiers():
    p = build_prompt("Jane Doe", "Acme Capital", "Austin")
    assert "Jane Doe" in p and "Acme Capital" in p and "Austin" in p
    assert "found=false" in p  # namesake guard instruction


def test_map_claims_full_profile():
    data = {
        "found": True,
        "linkedin_url": "linkedin.com/in/jane-doe",
        "current_title": "Partner",
        "current_employer": "Acme Capital",
        "location": "Austin, TX",
        "experience": [
            {"title": "Partner", "company": "Acme Capital", "start_year": "2020", "end_year": ""},
            {"title": "Analyst", "company": "TRS", "start_year": "2015", "end_year": "2020"},
        ],
        "education": [{"degree": "BBA", "school": "Texas A&M University"}],
    }
    rows = map_claims(data)
    kinds = {r.claim_type for r in rows}
    assert kinds == {"current_title", "current_employer", "location", "career_history", "education", "public_links"}
    assert all(r.extraction_method == EXTRACTION_METHOD for r in rows)
    # source_url normalized to https and attached
    assert all(r.source_url == "https://linkedin.com/in/jane-doe" for r in rows if r.source_url)
    # dated experience emits the quote form resume.ts parses first
    analyst = next(r for r in rows if r.value.startswith("Analyst"))
    assert analyst.quote == "2015 - 2020 Analyst @ TRS"
    # open-ended role -> "present"
    partner = next(r for r in rows if r.claim_type == "career_history" and r.value.startswith("Partner"))
    assert "(2020-present)" in partner.value


def test_map_claims_not_found_returns_empty():
    assert map_claims({"found": False, "current_employer": "X"}) == []
    assert map_claims({}) == []


def test_map_claims_handles_missing_url():
    rows = map_claims({"found": True, "current_employer": "Acme"})
    assert any(r.claim_type == "current_employer" for r in rows)
    # no url -> no LinkedIn public_link emitted
    assert not any(r.claim_type == "public_links" for r in rows)


class _Resp:
    def __init__(self, data, status="completed", error=None, credits=3):
        self.data = data
        self.status = status
        self.error = error
        self.credits_used = credits


class _Client:
    def __init__(self, resp=None, exc=None):
        self._resp = resp
        self._exc = exc

    def agent(self, **_):
        if self._exc:
            raise self._exc
        return self._resp


def test_fetch_success_maps_and_reports_credits():
    resp = _Resp({"found": True, "linkedin_url": "https://linkedin.com/in/x",
                  "current_employer": "Acme"}, credits=4)
    res = fetch_linkedin(_Client(resp), "Jane Doe", employer="Acme", city="Austin")
    assert res.found is True and res.credits_used == 4
    assert any(r.claim_type == "current_employer" for r in res.claim_rows)


def test_fetch_payment_required_propagates():
    client = _Client(exc=PaymentRequiredError("no credits"))
    with pytest.raises(PaymentRequiredError):
        fetch_linkedin(client, "Jane Doe")


def test_fetch_other_error_returns_empty():
    client = _Client(exc=RuntimeError("boom"))
    res = fetch_linkedin(client, "Jane Doe")
    assert res.claim_rows == () and res.found is False


def test_fetch_empty_name_skips():
    res = fetch_linkedin(_Client(), "   ")
    assert res.claim_rows == ()
