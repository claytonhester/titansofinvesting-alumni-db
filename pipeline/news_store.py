"""SQLite persistence for the curated "In the news" feed (Phase 2.5).

Raw press hits are collected as `news_mention` claims (date + headline + snippet
+ source) during enrichment. This table holds the CURATED layer the Haiku news
agent produces on top of them: a category, a one-line summary, and an importance
score — so the web "In the news" tab can group by category, lead with the most
important story, and show a clean summary instead of a raw scrape snippet.

One row per (person, article URL). Writes are replace-per-person so re-enriching
someone refreshes their curated set without duplicating. The pipeline owns the
writes; the web opens the same file READ-ONLY.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

# The fixed category vocabulary — kept in sync with web/lib/news-types.ts.
NEWS_CATEGORIES = (
    "Funding & Deals",
    "Leadership Moves",
    "Market Views",
    "Recognition",
    "Company News",
)
DEFAULT_CATEGORY = "Company News"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS news_curated (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id   INTEGER NOT NULL,
    source_url  TEXT    NOT NULL,
    headline    TEXT    NOT NULL,
    summary     TEXT    NOT NULL DEFAULT '',
    category    TEXT    NOT NULL DEFAULT 'Company News',
    date        TEXT    NOT NULL DEFAULT '',
    source_host TEXT    NOT NULL DEFAULT '',
    importance  REAL    NOT NULL DEFAULT 0.0,
    curated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, source_url),
    FOREIGN KEY (person_id) REFERENCES people (id)
);
CREATE INDEX IF NOT EXISTS idx_news_curated_person ON news_curated (person_id);
CREATE INDEX IF NOT EXISTS idx_news_curated_importance ON news_curated (importance DESC);
"""


@dataclass(frozen=True)
class CuratedNews:
    headline: str
    summary: str
    category: str
    date: str
    source_url: str
    source_host: str
    importance: float


def init_news_schema(conn: sqlite3.Connection) -> None:
    """Create the curated-news table. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)


def replace_curated_news(
    conn: sqlite3.Connection, person_id: int, items: list[CuratedNews]
) -> None:
    """Replace a person's curated news set atomically (delete then insert), so a
    re-run refreshes rather than duplicates."""
    conn.execute("DELETE FROM news_curated WHERE person_id = ?", (person_id,))
    conn.executemany(
        """
        INSERT OR IGNORE INTO news_curated (
            person_id, source_url, headline, summary, category, date,
            source_host, importance
        ) VALUES (:pid, :url, :head, :sum, :cat, :date, :host, :imp)
        """,
        [
            {
                "pid": person_id,
                "url": it.source_url,
                "head": it.headline,
                "sum": it.summary,
                "cat": it.category if it.category in NEWS_CATEGORIES else DEFAULT_CATEGORY,
                "date": it.date,
                "host": it.source_host,
                "imp": it.importance,
            }
            for it in items
        ],
    )
