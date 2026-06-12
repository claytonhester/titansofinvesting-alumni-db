"""Persistent cache for cross-industry role-level classifications.

This is what makes the seniority_v2 layer DYNAMIC rather than merely repeatable:
each distinct (title, employer) role is classified exactly once and remembered.
When enrichment later adds new people or titles, only the genuinely new roles hit
Haiku — everything else is a cache read, so reclassification stays near-free and
instant for the life of the project.

Keyed by (title_norm, employer_norm, version):
- title_norm / employer_norm — the normalized role identity (see seniority_v2._norm).
- version — the ladder/prompt generation. Bump LEVEL_VERSION in the runner to
  invalidate the whole cache and force a clean re-run when the rules change;
  old rows stay for audit but are no longer read.

Every row carries provenance — the label, whether it came from 'haiku' or the
'keyword' fallback, the model id, the sector hint used, and a timestamp — so any
classification can be explained after the fact without re-running the model.

The cache stores ONLY derived tags. It never touches `claims` / `person_company`;
the public-facing title is always the untouched ground truth.
"""
from __future__ import annotations

import sqlite3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_level_cache (
    title_norm     TEXT    NOT NULL,
    employer_norm  TEXT    NOT NULL,
    version        INTEGER NOT NULL,
    level          TEXT    NOT NULL,
    source         TEXT    NOT NULL DEFAULT '',   -- 'haiku' | 'keyword'
    model          TEXT    NOT NULL DEFAULT '',
    sector_hint    TEXT    NOT NULL DEFAULT '',
    classified_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (title_norm, employer_norm, version)
);

-- The materialized career TRAJECTORY: one row per (person, role) with its dates
-- AND its classified rung, ordered. This is what lets us map a person's climb
-- over the years (Analyst -> Associate -> VP -> MD) rather than only knowing
-- their peak. Derived from claims + person_company + role_level_cache; the
-- public title (`title`) is the untouched original, `level` is the derived tag.
-- Rewritten wholesale per person per run (delete + insert), so it always
-- reflects the current data and ladder version.
CREATE TABLE IF NOT EXISTS person_role_levels (
    person_id    INTEGER NOT NULL,
    seq          INTEGER NOT NULL,   -- 0-based order along the timeline (by start year)
    title        TEXT    NOT NULL,   -- original-cased, public-facing
    employer     TEXT    NOT NULL DEFAULT '',
    start_year   INTEGER,
    end_year     INTEGER,            -- NULL = ongoing / unknown
    is_current   INTEGER NOT NULL DEFAULT 0,
    level        TEXT    NOT NULL DEFAULT '',  -- '' / 'Non-title' = off the ladder
    level_index  INTEGER,            -- NULL when off the ladder
    version      INTEGER NOT NULL,
    PRIMARY KEY (person_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_role_levels_person ON person_role_levels (person_id);
"""


def init_role_level_schema(conn: sqlite3.Connection) -> None:
    """Create the cache + trajectory tables. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)


def replace_person_trajectory(
    conn: sqlite3.Connection,
    person_id: int,
    version: int,
    rows: list[tuple[str, str, int | None, int | None, int, str, int | None]],
) -> None:
    """Rewrite one person's timeline. Each row is
    (title, employer, start_year, end_year, is_current, level, level_index),
    already ordered. Delete-then-insert keeps it idempotent and drift-free.
    Caller commits."""
    conn.execute("DELETE FROM person_role_levels WHERE person_id = ?", (person_id,))
    conn.executemany(
        "INSERT INTO person_role_levels "
        "(person_id, seq, title, employer, start_year, end_year, is_current, "
        " level, level_index, version) VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            (person_id, seq, t, e, sy, ey, isc, lvl, lidx, version)
            for seq, (t, e, sy, ey, isc, lvl, lidx) in enumerate(rows)
        ],
    )


def load_cached(
    conn: sqlite3.Connection, version: int
) -> dict[tuple[str, str], str]:
    """Every cached label at this version, as {(title_norm, employer_norm): level}.
    A miss is simply an absent key — the caller classifies those and writes them
    back with upsert_levels."""
    rows = conn.execute(
        "SELECT title_norm, employer_norm, level FROM role_level_cache "
        "WHERE version = ?",
        (version,),
    ).fetchall()
    return {(r["title_norm"], r["employer_norm"]): r["level"] for r in rows}


def upsert_levels(
    conn: sqlite3.Connection,
    version: int,
    rows: list[tuple[str, str, str, str, str, str]],
) -> None:
    """Write classifications. Each row is
    (title_norm, employer_norm, level, source, model, sector_hint).
    Idempotent on (title_norm, employer_norm, version): re-classifying a role
    replaces its entry and refreshes the timestamp. Caller commits."""
    conn.executemany(
        """
        INSERT INTO role_level_cache
            (title_norm, employer_norm, version, level, source, model, sector_hint,
             classified_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
        ON CONFLICT (title_norm, employer_norm, version) DO UPDATE SET
            level         = excluded.level,
            source        = excluded.source,
            model         = excluded.model,
            sector_hint   = excluded.sector_hint,
            classified_at = excluded.classified_at
        """,
        [(t, e, version, lvl, src, mdl, sec) for (t, e, lvl, src, mdl, sec) in rows],
    )
