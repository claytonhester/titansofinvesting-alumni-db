"""Person ↔ firm links across the WHOLE career history (not just the current job).

Each row ties one alumnus to one firm (by domain) with the role title and the
years they were there, flagged current vs past. This is what powers a company
page's institutional memory: "Titans here now" + "Titans who were here, when, and
as what". Populated from PDL experience[] (domains free on a match) and, for the
existing cohort, by name-matching career_history claims to enriched firms.

Writes are replace-per-person (idempotent); the web reads READ-ONLY.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_company (
    person_id     INTEGER NOT NULL,
    domain        TEXT    NOT NULL,            -- join key to companies(domain)
    company_name  TEXT    NOT NULL DEFAULT '',
    title         TEXT    NOT NULL DEFAULT '',
    start_year    INTEGER,
    end_year      INTEGER,                     -- NULL when current/ongoing
    is_current    INTEGER NOT NULL DEFAULT 0,
    source        TEXT    NOT NULL DEFAULT '', -- 'pdl' | 'career-match'
    FOREIGN KEY (person_id) REFERENCES people (id)
);
CREATE INDEX IF NOT EXISTS idx_person_company_domain ON person_company (domain);
CREATE INDEX IF NOT EXISTS idx_person_company_person ON person_company (person_id);
"""


@dataclass(frozen=True)
class PersonCompany:
    person_id: int
    domain: str
    company_name: str = ""
    title: str = ""
    start_year: int | None = None
    end_year: int | None = None
    is_current: bool = False
    source: str = ""


def init_person_company_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)


def replace_person_companies(
    conn: sqlite3.Connection, person_id: int, links: list[PersonCompany]
) -> None:
    """Idempotent on person_id: re-enriching a person replaces all their firm links.
    Rows with an empty domain are skipped (can't link to a company page)."""
    conn.execute("DELETE FROM person_company WHERE person_id = ?", (person_id,))
    rows = [l for l in links if l.domain]
    if not rows:
        return
    conn.executemany(
        """
        INSERT INTO person_company (
            person_id, domain, company_name, title, start_year, end_year,
            is_current, source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                l.person_id, l.domain, l.company_name, l.title,
                l.start_year, l.end_year, 1 if l.is_current else 0, l.source,
            )
            for l in rows
        ],
    )


def linked_domains(conn: sqlite3.Connection) -> set[str]:
    """All distinct firm domains referenced by any alumnus (current OR past) — the
    set company_enrich should ensure are enriched so their pages exist."""
    try:
        return {r[0] for r in conn.execute("SELECT DISTINCT domain FROM person_company")}
    except sqlite3.OperationalError:
        return set()
