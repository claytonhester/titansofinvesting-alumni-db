"""Unit tests for company_store — in-memory SQLite, the cache contract."""
from __future__ import annotations

import sqlite3

import pytest

from company_store import (
    CompanyRecord,
    existing_domains,
    get_company,
    init_company_schema,
    upsert_company,
)


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_company_schema(c)
    return c


@pytest.mark.unit
def test_upsert_and_get_roundtrip() -> None:
    c = _conn()
    rec = CompanyRecord(
        domain="bcg.com", name="BCG", industry="management consulting",
        size="10001+", employee_count=32637, company_type="private", founded=1963,
        hq_location="boston", tags=["consulting"], likelihood=6, matched=True,
    )
    upsert_company(c, rec)
    got = get_company(c, "bcg.com")
    assert got is not None
    assert got.name == "BCG" and got.employee_count == 32637 and got.matched is True
    assert got.tags == ["consulting"]


@pytest.mark.unit
def test_existing_domains_is_the_cache_key_set() -> None:
    """existing_domains drives the 'never enrich twice' skip — it must include both
    matched firms and no-match sentinels."""
    c = _conn()
    upsert_company(c, CompanyRecord(domain="bcg.com", name="BCG", matched=True))
    upsert_company(c, CompanyRecord(domain="tiny.co", matched=False))  # sentinel
    assert existing_domains(c) == {"bcg.com", "tiny.co"}


@pytest.mark.unit
def test_upsert_is_idempotent_on_domain() -> None:
    c = _conn()
    upsert_company(c, CompanyRecord(domain="x.com", name="Old", matched=True))
    upsert_company(c, CompanyRecord(domain="x.com", name="New", employee_count=10, matched=True))
    assert len(existing_domains(c)) == 1
    assert get_company(c, "x.com").name == "New"


@pytest.mark.unit
def test_missing_company_returns_none() -> None:
    assert get_company(_conn(), "absent.com") is None
