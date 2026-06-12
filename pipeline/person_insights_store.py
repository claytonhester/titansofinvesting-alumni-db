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
    job_sub_function        TEXT,
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
    first_sector        TEXT,              -- sector of the FIRST employer (Origins)
    left_texas          INTEGER,           -- 1 / 0 / NULL (unknown)
    -- Profile-quality score (0-100), owned by compute_completeness.py: written
    -- by its UPDATE during finalize, deliberately NOT part of the enrichment
    -- upsert so a re-enrich can't zero it before the next finalize recomputes.
    completeness_score  INTEGER NOT NULL DEFAULT 0,
    employer_domain     TEXT    NOT NULL DEFAULT '',  -- join key to companies(domain)
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
    job_sub_function: str = ""
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
    first_sector: str = ""
    left_texas: bool | None = None
    employer_domain: str = ""
    model: str = ""
    # Read-only here: written by compute_completeness.py, not the upsert.
    completeness_score: int = 0


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a DB was first created. CREATE TABLE IF NOT
    EXISTS won't alter an existing table, so additive columns are migrated here.
    Idempotent: only adds what's missing."""
    have = {r[1] for r in conn.execute("PRAGMA table_info(person_insights)")}
    additive = {
        "job_sub_function": "TEXT",
        "employer_domain": "TEXT NOT NULL DEFAULT ''",
        "first_sector": "TEXT",
        "completeness_score": "INTEGER NOT NULL DEFAULT 0",
        # Two-pass enrichment: the base sweep marks who needs a deep (Firecrawl)
        # pass. Owned by compute_completeness.py (set/cleared by its UPDATE,
        # like completeness_score) — deliberately NOT in the enrichment upsert,
        # so a re-enrich can't clobber the flag before finalize recomputes it.
        "needs_deep_search": "INTEGER NOT NULL DEFAULT 0",
        "deep_search_reason": "TEXT NOT NULL DEFAULT ''",
        # Sticky: set to 1 once a --needs-deep pass has processed this person, so
        # the queue DRAINS — a genuinely short career (e.g. 2 roles) or a ghost
        # the read can't lift won't re-flag and re-burn ~$0.30/run forever. An
        # operator resets it to 0 to force a deliberate re-sweep.
        "deep_search_done": "INTEGER NOT NULL DEFAULT 0",
        # Cross-industry seniority (seniority_v2). The derived rung + the two
        # product thresholds, computed from the role_level_cache. These live
        # ALONGSIDE the legacy reached_md / years_to_md (which stay untouched for
        # the existing web) — the public title is never overwritten; this is a
        # separate normalized tag. level_version stamps which ladder produced
        # them so a ladder change is auditable and forces a clean recompute.
        "peak_level": "TEXT NOT NULL DEFAULT ''",
        "reached_manager": "INTEGER NOT NULL DEFAULT 0",
        "reached_senior_leadership": "INTEGER NOT NULL DEFAULT 0",
        "years_to_manager": "INTEGER",
        "years_to_senior_leadership": "INTEGER",
        "level_model": "TEXT NOT NULL DEFAULT ''",
        "level_version": "INTEGER NOT NULL DEFAULT 0",
    }
    for col, decl in additive.items():
        if col not in have:
            conn.execute(f"ALTER TABLE person_insights ADD COLUMN {col} {decl}")


def init_person_insights_schema(conn: sqlite3.Connection) -> None:
    """Create the per-person insights table, then apply additive migrations.
    Safe to call repeatedly."""
    conn.executescript(_SCHEMA)
    _ensure_columns(conn)


def upsert_person_insight(conn: sqlite3.Connection, row: PersonInsight) -> None:
    """Idempotent on person_id: re-enriching a person supersedes their row."""
    conn.execute(
        """
        INSERT INTO person_insights (
            person_id, grad_year, grad_year_source, first_employer,
            on_buy_side, reached_md, founder_partner, still_first_firm,
            started_sell_side, current_industry, current_company_size,
            job_function, job_sub_function, pdl_seniority, current_role_start_year,
            years_experience, linkedin_connections, tenure_years, years_to_md,
            num_employers, has_advanced_degree, current_sector, first_sector,
            left_texas, employer_domain, model, classified_at
        ) VALUES (
            :pid, :gy, :gys, :fe, :bs, :md, :fp, :sff, :sss, :ind, :size,
            :func, :subfunc, :sen, :rsy, :yexp, :conn, :ten, :ytm, :nemp, :adv, :sec,
            :fsec, :ltx, :empdom, :model, datetime('now')
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
            job_sub_function        = excluded.job_sub_function,
            pdl_seniority           = excluded.pdl_seniority,
            current_role_start_year = excluded.current_role_start_year,
            years_experience        = excluded.years_experience,
            linkedin_connections    = excluded.linkedin_connections,
            tenure_years            = excluded.tenure_years,
            years_to_md             = excluded.years_to_md,
            num_employers           = excluded.num_employers,
            has_advanced_degree     = excluded.has_advanced_degree,
            current_sector          = excluded.current_sector,
            first_sector            = excluded.first_sector,
            left_texas              = excluded.left_texas,
            employer_domain         = excluded.employer_domain,
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
            "subfunc": row.job_sub_function,
            "sen": row.pdl_seniority,
            "rsy": row.current_role_start_year,
            "yexp": row.years_experience,
            "conn": row.linkedin_connections,
            "ten": row.tenure_years,
            "ytm": row.years_to_md,
            "nemp": row.num_employers,
            "adv": 1 if row.has_advanced_degree else 0,
            "sec": row.current_sector,
            "fsec": row.first_sector,
            "ltx": None if row.left_texas is None else (1 if row.left_texas else 0),
            "empdom": row.employer_domain or "",
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
        job_sub_function=(row["job_sub_function"] if "job_sub_function" in row.keys() else "") or "",
        pdl_seniority=row["pdl_seniority"] or "",
        current_role_start_year=row["current_role_start_year"],
        years_experience=row["years_experience"],
        linkedin_connections=row["linkedin_connections"],
        tenure_years=row["tenure_years"],
        years_to_md=row["years_to_md"],
        num_employers=row["num_employers"],
        has_advanced_degree=bool(row["has_advanced_degree"]),
        current_sector=row["current_sector"] or "",
        first_sector=(row["first_sector"] if "first_sector" in row.keys() else "") or "",
        left_texas=None if lt is None else bool(lt),
        employer_domain=(row["employer_domain"] if "employer_domain" in row.keys() else "") or "",
        model=row["model"],
        completeness_score=(row["completeness_score"] if "completeness_score" in row.keys() else 0) or 0,
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
