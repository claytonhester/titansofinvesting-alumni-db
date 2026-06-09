"""Integration tests for the curated-news store."""
from __future__ import annotations

import sqlite3

import pytest

from db import init_schema
from news_store import CuratedNews, init_news_schema, replace_curated_news


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    init_schema(c)
    init_news_schema(c)
    return c


def _item(url="https://x.com/a", cat="Funding & Deals", imp=0.9):
    return CuratedNews(
        headline="H", summary="S", category=cat, date="2026-05-01",
        source_url=url, source_host="x.com", importance=imp,
    )


def test_replace_and_read(conn):
    replace_curated_news(conn, 1, [_item()])
    rows = conn.execute("SELECT * FROM news_curated WHERE person_id=1").fetchall()
    assert len(rows) == 1
    assert rows[0]["category"] == "Funding & Deals"
    assert rows[0]["importance"] == 0.9


def test_replace_is_per_person_idempotent(conn):
    replace_curated_news(conn, 1, [_item(url="https://x.com/a"), _item(url="https://x.com/b")])
    replace_curated_news(conn, 1, [_item(url="https://x.com/c")])  # replaces
    rows = conn.execute("SELECT source_url FROM news_curated WHERE person_id=1").fetchall()
    assert [r["source_url"] for r in rows] == ["https://x.com/c"]


def test_invalid_category_coerced_to_default(conn):
    replace_curated_news(conn, 2, [_item(cat="Sports")])
    row = conn.execute("SELECT category FROM news_curated WHERE person_id=2").fetchone()
    assert row["category"] == "Company News"


def test_dedup_same_url_within_person(conn):
    # two items, same URL -> UNIQUE(person_id, source_url) keeps one
    replace_curated_news(conn, 3, [_item(url="https://x.com/same"), _item(url="https://x.com/same")])
    n = conn.execute("SELECT COUNT(*) AS n FROM news_curated WHERE person_id=3").fetchone()["n"]
    assert n == 1
