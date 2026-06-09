"""Unit tests for company_enrich — HTTP mocked, no spend.

Covers: top-level field parse (Company Base puts fields at the root, not under
'data'), no-match sentinel vs transient-None, domain canonicalization, and the
employer-domain resolver (incl. acronym domains like bpc.com)."""
from __future__ import annotations

import httpx
import pytest

from company_enrich import (
    _bare_domain,
    enrich_company,
    resolve_employer_domain,
)


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


_BCG = {
    "status": 200, "display_name": "Boston Consulting Group (BCG)",
    "industry": "management consulting", "industry_v2": "business consulting and services",
    "size": "10001+", "employee_count": 32637, "type": "private", "founded": 1963,
    "location": {"name": "boston, massachusetts, united states"},
    "linkedin_url": "linkedin.com/company/boston-consulting-group",
    "summary": "BCG partners with leaders in business and society.",
    "tags": ["consulting", "strategy"], "likelihood": 6,
}


@pytest.mark.unit
def test_bare_domain_canonicalizes() -> None:
    assert _bare_domain("https://www.bcg.com/path") == "bcg.com"
    assert _bare_domain("sageadvisory.com") == "sageadvisory.com"
    assert _bare_domain("HTTP://WWW.JPMorganChase.com/") == "jpmorganchase.com"
    assert _bare_domain("") == ""


@pytest.mark.unit
def test_enrich_parses_top_level_fields() -> None:
    rec = enrich_company(_client(lambda r: httpx.Response(200, json=_BCG)), "k", "bcg.com")
    assert rec is not None and rec.matched is True
    assert rec.name == "Boston Consulting Group (BCG)"
    assert rec.industry == "management consulting"
    assert rec.size == "10001+" and rec.employee_count == 32637
    assert rec.company_type == "private" and rec.founded == 1963
    assert rec.hq_location.startswith("boston")
    assert rec.tags == ["consulting", "strategy"]


@pytest.mark.unit
def test_empty_200_is_a_no_match_sentinel() -> None:
    """A confident-but-empty response (name-only weak match) caches as a sentinel
    so we never re-pay to rediscover a firm PDL lacks."""
    body = {"status": 200, "likelihood": 3}
    rec = enrich_company(_client(lambda r: httpx.Response(200, json=body)), "k", "tiny.co")
    assert rec is not None and rec.matched is False and rec.domain == "tiny.co"


@pytest.mark.unit
def test_404_is_a_no_match_sentinel() -> None:
    rec = enrich_company(_client(lambda r: httpx.Response(404, json={})), "k", "nope.com")
    assert rec is not None and rec.matched is False


@pytest.mark.unit
def test_transient_failure_returns_none_not_sentinel() -> None:
    """A 5xx/outage must NOT be cached (so it retries next run) — returns None."""
    rec = enrich_company(
        _client(lambda r: httpx.Response(503)), "k", "bcg.com",
        attempts=2, backoff_base=0.0,
    )
    assert rec is None


@pytest.mark.unit
def test_resolve_domain_by_token_overlap() -> None:
    got = resolve_employer_domain(
        "Sage Advisory Services Ltd Co",
        ["https://etf.com/x", "https://www.sageadvisory.com/team"],
    )
    assert got == "sageadvisory.com"


@pytest.mark.unit
def test_resolve_domain_by_acronym() -> None:
    got = resolve_employer_domain(
        "Brighton Park Capital", ["https://www.bpc.com/insights/promotions"]
    )
    assert got == "bpc.com"


@pytest.mark.unit
def test_resolve_ignores_generic_geo_token() -> None:
    """'texas' must not match texastaxpayers.com (a news source) to employer
    'Teacher Retirement System of Texas' — a real false positive we hit."""
    got = resolve_employer_domain(
        "Teacher Retirement System of Texas",
        ["https://www.texastaxpayers.com/article", "https://trs.texas.gov/book"],
    )
    # trs.texas.gov resolves via the TRS acronym; texastaxpayers.com is rejected.
    assert got == "trs.texas.gov"


@pytest.mark.unit
def test_resolve_drops_aggregators_and_social() -> None:
    got = resolve_employer_domain(
        "Acme Capital",
        ["https://theorg.com/acme", "https://linkedin.com/x", "https://wiza.co/a"],
    )
    assert got == ""
