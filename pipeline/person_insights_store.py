"""SQLite persistence for the per-person insights classification (Phase 2.5).

Sits between the per-claim grain (`claims`) and the cohort roll-up
(`insights_snapshot`): exactly ONE row per person holding the derived facts the
aggregate KPIs are built from —

    grad_year / grad_year_source   how we know when they graduated
    first_employer                 first post-grad employer (Origins, "first firm")
    on_buy_side                    in an investing seat now
    reached_md                     ever reached MD / Partner / C-suite
    founder_partner                runs their own fund or holds a partner seat
    still_first_firm               current employer == first post-grad employer

The four flags are produced by a Haiku classifier (kpi_classify); grad_year and
first_employer are deterministic (grad_year / career_analysis). The "Reached MD
or above" fairness rule (only counts against people graduated >= 10 years ago)
is applied at roll-up time over these rows, NOT stored here — this table records
the per-person truth, the rollup decides how to fold it.

Writes are idempotent on person_id: re-enriching a person replaces their row.
The pipeline owns writes; the web opens the same file READ-ONLY.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass

_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_insights (
    person_id        INTEGER PRIMARY KEY,
    grad_year        INTEGER,
    grad_year_source TEXT    NOT NULL DEFAULT '',
    first_employer   TEXT    NOT NULL DEFAULT '',
    on_buy_side      INTEGER NOT NULL DEFAULT 0,
    reached_md       INTEGER NOT NULL DEFAULT 0,
    founder_partner  INTEGER NOT NULL DEFAULT 0,
    still_first_firm INTEGER NOT NULL DEFAULT 0,
    model            TEXT    NOT NULL DEFAULT '',
    classified_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (person_id) REFERENCES people (id)
);
"""


@dataclass(frozen=True)
class PersonInsight:
    person_id: int
    grad_year: int | None
    grad_year_source: str
    first_employer: str
    on_buy_side: bool
    reached_md: bool
    founder_partner: bool
    still_first_firm: bool
    model: str = ""


def init_person_insights_schema(conn: sqlite3.Connection) -> None:
    """Create the per-person insights table. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)


def upsert_person_insight(conn: sqlite3.Connection, row: PersonInsight) -> None:
    """Idempotent on person_id: re-enriching a person supersedes their row."""
    conn.execute(
        """
        INSERT INTO person_insights (
            person_id, grad_year, grad_year_source, first_employer,
            on_buy_side, reached_md, founder_partner, still_first_firm, model,
            classified_at
        ) VALUES (
            :pid, :gy, :gys, :fe, :bs, :md, :fp, :sff, :model, datetime('now')
        )
        ON CONFLICT (person_id) DO UPDATE SET
            grad_year        = excluded.grad_year,
            grad_year_source = excluded.grad_year_source,
            first_employer   = excluded.first_employer,
            on_buy_side      = excluded.on_buy_side,
            reached_md       = excluded.reached_md,
            founder_partner  = excluded.founder_partner,
            still_first_firm = excluded.still_first_firm,
            model            = excluded.model,
            classified_at    = datetime('now')
        """,
        {
            "pid": row.person_id,
            "gy": row.grad_year,
            "gys": row.grad_year_source,
            "fe": row.first_employer,
            "bs": 1 if row.on_buy_side else 0,
            "md": 1 if row.reached_md else 0,
            "fp": 1 if row.founder_partner else 0,
            "sff": 1 if row.still_first_firm else 0,
            "model": row.model,
        },
    )


def _to_insight(row: sqlite3.Row) -> PersonInsight:
    return PersonInsight(
        person_id=row["person_id"],
        grad_year=row["grad_year"],
        grad_year_source=row["grad_year_source"],
        first_employer=row["first_employer"],
        on_buy_side=bool(row["on_buy_side"]),
        reached_md=bool(row["reached_md"]),
        founder_partner=bool(row["founder_partner"]),
        still_first_firm=bool(row["still_first_firm"]),
        model=row["model"],
    )


def get_person_insight(
    conn: sqlite3.Connection, person_id: int
) -> PersonInsight | None:
    row = conn.execute(
        "SELECT * FROM person_insights WHERE person_id = ?", (person_id,)
    ).fetchone()
    return _to_insight(row) if row else None


def all_person_insights(conn: sqlite3.Connection) -> list[PersonInsight]:
    """Every classified person — the input to the cohort KPI roll-up."""
    rows = conn.execute("SELECT * FROM person_insights ORDER BY person_id").fetchall()
    return [_to_insight(r) for r in rows]
