"""Integration tests for the person_insights store (schema, upsert, reads)."""
from __future__ import annotations

import sqlite3

import pytest

from db import init_schema
from person_insights_store import (
    PersonInsight,
    all_person_insights,
    get_person_insight,
    init_person_insights_schema,
    upsert_person_insight,
)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    init_person_insights_schema(c)
    return c


def _insight(pid=1, **kw):
    base = dict(
        person_id=pid, grad_year=2014, grad_year_source="class-map",
        first_employer="Goldman", on_buy_side=True, reached_md=True,
        founder_partner=False, still_first_firm=False, started_sell_side=True,
        model="haiku",
    )
    base.update(kw)
    return PersonInsight(**base)


def test_upsert_and_read_roundtrip(conn):
    upsert_person_insight(conn, _insight())
    got = get_person_insight(conn, 1)
    assert got is not None
    assert got.grad_year == 2014 and got.grad_year_source == "class-map"
    assert got.first_employer == "Goldman"
    assert got.on_buy_side is True and got.reached_md is True
    assert got.founder_partner is False and got.still_first_firm is False
    assert got.started_sell_side is True


def test_upsert_is_idempotent_on_person(conn):
    upsert_person_insight(conn, _insight(reached_md=True))
    upsert_person_insight(conn, _insight(reached_md=False, first_employer="Bain"))
    rows = all_person_insights(conn)
    assert len(rows) == 1  # replaced, not duplicated
    assert rows[0].reached_md is False and rows[0].first_employer == "Bain"


def test_get_missing_returns_none(conn):
    assert get_person_insight(conn, 999) is None


def test_grad_year_nullable(conn):
    upsert_person_insight(conn, _insight(pid=2, grad_year=None, grad_year_source=""))
    got = get_person_insight(conn, 2)
    assert got.grad_year is None and got.grad_year_source == ""


def test_collected_and_derived_fields_roundtrip(conn):
    upsert_person_insight(conn, _insight(
        pid=5, current_industry="investment management", current_company_size="1001-5000",
        job_function="finance", pdl_seniority="director, partner",
        current_role_start_year=2018, years_experience=14, linkedin_connections=832,
        tenure_years=8, years_to_md=8, num_employers=3, has_advanced_degree=True,
        current_sector="Private Equity & Credit", left_texas=True,
    ))
    got = get_person_insight(conn, 5)
    assert got.current_industry == "investment management"
    assert got.current_company_size == "1001-5000"
    assert got.job_function == "finance" and got.pdl_seniority == "director, partner"
    assert got.current_role_start_year == 2018 and got.years_experience == 14
    assert got.linkedin_connections == 832 and got.tenure_years == 8
    assert got.years_to_md == 8 and got.num_employers == 3
    assert got.has_advanced_degree is True
    assert got.current_sector == "Private Equity & Credit"
    assert got.left_texas is True


def test_left_texas_tristate_roundtrip(conn):
    upsert_person_insight(conn, _insight(pid=6, left_texas=None))
    assert get_person_insight(conn, 6).left_texas is None
    upsert_person_insight(conn, _insight(pid=7, left_texas=False))
    assert get_person_insight(conn, 7).left_texas is False


def test_all_person_insights_ordered(conn):
    upsert_person_insight(conn, _insight(pid=3))
    upsert_person_insight(conn, _insight(pid=1))
    upsert_person_insight(conn, _insight(pid=2))
    assert [r.person_id for r in all_person_insights(conn)] == [1, 2, 3]
