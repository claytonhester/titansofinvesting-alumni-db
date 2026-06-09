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
    on_buy_side       INTEGER NOT NULL DEFAULT 0,
    reached_md        INTEGER NOT NULL DEFAULT 0,
    founder_partner   INTEGER NOT NULL DEFAULT 0,
    still_first_firm  INTEGER NOT NULL DEFAULT 0,
    started_sell_side INTEGER NOT NULL DEFAULT 0,
    -- Collected from the PDL match (already paid for); empty/NULL when absent.
    current_industry        TEXT,
    current_company_size    TEXT,
    job_function            TEXT,
    pdl_seniority           TEXT,
    current_role_start_year INTEGER,
    years_experience        INTEGER,
    linkedin_connections    INTEGER,
    -- Derived metrics (computed from claims + grad_year).
    tenure_years        INTEGER,
    years_to_md         INTEGER,
    num_employers       INTEGER,
    has_advanced_degree INTEGER NOT NULL DEFAULT 0,
    current_sector      TEXT,
    left_texas          INTEGER,           -- 1 / 0 / NULL (unknown)
    model             TEXT    NOT NULL DEFAULT '',
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
    started_sell_side: bool = False
    # Collected (PDL).
    current_industry: str = ""
    current_company_size: str = ""
    job_function: str = ""
    pdl_seniority: str = ""
    current_role_start_year: int | None = None
    years_experience: int | None = None
    linkedin_connections: int | None = None
    # Derived.
    tenure_years: int | None = None
    years_to_md: int | None = None
    num_employers: int | None = None
    has_advanced_degree: bool = False
    current_sector: str = ""
    left_texas: bool | None = None
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
            on_buy_side, reached_md, founder_partner, still_first_firm,
            started_sell_side, current_industry, current_company_size,
            job_function, pdl_seniority, current_role_start_year,
            years_experience, linkedin_connections, tenure_years, years_to_md,
            num_employers, has_advanced_degree, current_sector, left_texas,
            model, classified_at
        ) VALUES (
            :pid, :gy, :gys, :fe, :bs, :md, :fp, :sff, :sss, :ind, :size,
            :func, :sen, :rsy, :yexp, :conn, :ten, :ytm, :nemp, :adv, :sec,
            :ltx, :model, datetime('now')
        )
        ON CONFLICT (person_id) DO UPDATE SET
            grad_year               = excluded.grad_year,
            grad_year_source        = excluded.grad_year_source,
            first_employer          = excluded.first_employer,
            on_buy_side             = excluded.on_buy_side,
            reached_md              = excluded.reached_md,
            founder_partner         = excluded.founder_partner,
            still_first_firm        = excluded.still_first_firm,
            started_sell_side       = excluded.started_sell_side,
            current_industry        = excluded.current_industry,
            current_company_size    = excluded.current_company_size,
            job_function            = excluded.job_function,
            pdl_seniority           = excluded.pdl_seniority,
            current_role_start_year = excluded.current_role_start_year,
            years_experience        = excluded.years_experience,
            linkedin_connections    = excluded.linkedin_connections,
            tenure_years            = excluded.tenure_years,
            years_to_md             = excluded.years_to_md,
            num_employers           = excluded.num_employers,
            has_advanced_degree     = excluded.has_advanced_degree,
            current_sector          = excluded.current_sector,
            left_texas              = excluded.left_texas,
            model                   = excluded.model,
            classified_at           = datetime('now')
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
            "sss": 1 if row.started_sell_side else 0,
            "ind": row.current_industry,
            "size": row.current_company_size,
            "func": row.job_function,
            "sen": row.pdl_seniority,
            "rsy": row.current_role_start_year,
            "yexp": row.years_experience,
            "conn": row.linkedin_connections,
            "ten": row.tenure_years,
            "ytm": row.years_to_md,
            "nemp": row.num_employers,
            "adv": 1 if row.has_advanced_degree else 0,
            "sec": row.current_sector,
            "ltx": None if row.left_texas is None else (1 if row.left_texas else 0),
            "model": row.model,
        },
    )


def _to_insight(row: sqlite3.Row) -> PersonInsight:
    lt = row["left_texas"]
    return PersonInsight(
        person_id=row["person_id"],
        grad_year=row["grad_year"],
        grad_year_source=row["grad_year_source"],
        first_employer=row["first_employer"],
        on_buy_side=bool(row["on_buy_side"]),
        reached_md=bool(row["reached_md"]),
        founder_partner=bool(row["founder_partner"]),
        still_first_firm=bool(row["still_first_firm"]),
        started_sell_side=bool(row["started_sell_side"]),
        current_industry=row["current_industry"] or "",
        current_company_size=row["current_company_size"] or "",
        job_function=row["job_function"] or "",
        pdl_seniority=row["pdl_seniority"] or "",
        current_role_start_year=row["current_role_start_year"],
        years_experience=row["years_experience"],
        linkedin_connections=row["linkedin_connections"],
        tenure_years=row["tenure_years"],
        years_to_md=row["years_to_md"],
        num_employers=row["num_employers"],
        has_advanced_degree=bool(row["has_advanced_degree"]),
        current_sector=row["current_sector"] or "",
        left_texas=None if lt is None else bool(lt),
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
