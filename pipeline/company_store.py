"""SQLite persistence for firm-level (company) enrichment.

A firm is enriched ONCE, ever, and shared by every alumnus who works there. The
table is keyed by canonical domain, so the enrichment pass can skip any firm
already present (`existing_domains`) — we never spend a credit on the same company
twice. Alumni link to a row via `person_insights.employer_domain`.

Lives in the same titans.db as everything else (the web reads it READ-ONLY); the
pipeline owns writes. Writes are idempotent on domain.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    domain          TEXT PRIMARY KEY,      -- canonical bare host, e.g. "bcg.com"
    name            TEXT NOT NULL DEFAULT '',
    industry        TEXT NOT NULL DEFAULT '',
    industry_v2     TEXT NOT NULL DEFAULT '',
    size            TEXT NOT NULL DEFAULT '',   -- PDL range, e.g. "51-200"
    employee_count  INTEGER,
    company_type    TEXT NOT NULL DEFAULT '',   -- public / private / ...
    ticker          TEXT NOT NULL DEFAULT '',
    founded         INTEGER,
    hq_location     TEXT NOT NULL DEFAULT '',
    linkedin_url    TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    tags            TEXT NOT NULL DEFAULT '',    -- comma-joined
    likelihood      INTEGER,
    matched         INTEGER NOT NULL DEFAULT 0,  -- 1 = real firmographics, 0 = no-match sentinel
    enriched_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


@dataclass(frozen=True)
class CompanyRecord:
    domain: str
    name: str = ""
    industry: str = ""
    industry_v2: str = ""
    size: str = ""
    employee_count: int | None = None
    company_type: str = ""
    ticker: str = ""
    founded: int | None = None
    hq_location: str = ""
    linkedin_url: str = ""
    summary: str = ""
    tags: list[str] = field(default_factory=list)
    likelihood: int | None = None
    matched: bool = False


def init_company_schema(conn: sqlite3.Connection) -> None:
    """Create the companies table. Safe to call repeatedly."""
    conn.executescript(_SCHEMA)


def existing_domains(conn: sqlite3.Connection) -> set[str]:
    """Domains already enriched (match OR recorded no-match) — the cache key set.
    The pass skips these so a firm is never enriched twice. Includes no-match
    sentinels so we don't re-pay to rediscover a firm PDL doesn't have."""
    try:
        return {r[0] for r in conn.execute("SELECT domain FROM companies")}
    except sqlite3.OperationalError:
        return set()


def upsert_company(conn: sqlite3.Connection, rec: CompanyRecord) -> None:
    """Idempotent on domain."""
    conn.execute(
        """
        INSERT INTO companies (
            domain, name, industry, industry_v2, size, employee_count,
            company_type, ticker, founded, hq_location, linkedin_url, summary,
            tags, likelihood, matched, enriched_at
        ) VALUES (
            :domain, :name, :industry, :industry_v2, :size, :employee_count,
            :company_type, :ticker, :founded, :hq_location, :linkedin_url, :summary,
            :tags, :likelihood, :matched, datetime('now')
        )
        ON CONFLICT (domain) DO UPDATE SET
            name=excluded.name, industry=excluded.industry,
            industry_v2=excluded.industry_v2, size=excluded.size,
            employee_count=excluded.employee_count, company_type=excluded.company_type,
            ticker=excluded.ticker, founded=excluded.founded,
            hq_location=excluded.hq_location, linkedin_url=excluded.linkedin_url,
            summary=excluded.summary, tags=excluded.tags,
            likelihood=excluded.likelihood, matched=excluded.matched,
            enriched_at=datetime('now')
        """,
        {
            "domain": rec.domain,
            "name": rec.name,
            "industry": rec.industry,
            "industry_v2": rec.industry_v2,
            "size": rec.size,
            "employee_count": rec.employee_count,
            "company_type": rec.company_type,
            "ticker": rec.ticker,
            "founded": rec.founded,
            "hq_location": rec.hq_location,
            "linkedin_url": rec.linkedin_url,
            "summary": rec.summary,
            "tags": ",".join(rec.tags),
            "likelihood": rec.likelihood,
            "matched": 1 if rec.matched else 0,
        },
    )


def get_company(conn: sqlite3.Connection, domain: str) -> CompanyRecord | None:
    row = conn.execute(
        "SELECT * FROM companies WHERE domain = ?", (domain,)
    ).fetchone()
    if not row:
        return None
    return CompanyRecord(
        domain=row["domain"],
        name=row["name"] or "",
        industry=row["industry"] or "",
        industry_v2=row["industry_v2"] or "",
        size=row["size"] or "",
        employee_count=row["employee_count"],
        company_type=row["company_type"] or "",
        ticker=row["ticker"] or "",
        founded=row["founded"],
        hq_location=row["hq_location"] or "",
        linkedin_url=row["linkedin_url"] or "",
        summary=row["summary"] or "",
        tags=[t for t in (row["tags"] or "").split(",") if t],
        likelihood=row["likelihood"],
        matched=bool(row["matched"]),
    )
