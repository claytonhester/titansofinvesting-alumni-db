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
        founder_partner=False, still_first_firm=False, model="haiku",
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


def test_all_person_insights_ordered(conn):
    upsert_person_insight(conn, _insight(pid=3))
    upsert_person_insight(conn, _insight(pid=1))
    upsert_person_insight(conn, _insight(pid=2))
    assert [r.person_id for r in all_person_insights(conn)] == [1, 2, 3]
