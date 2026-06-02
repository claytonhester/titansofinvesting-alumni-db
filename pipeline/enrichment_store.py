"""SQLite persistence for Stage 2 enrichment.

Sits alongside the Stage-1 `people` table (see db.py) and shares the same
read-only-web / offline-batch-write boundary. Four tables, each with a single
job:

- `person_sources`   — evidence pointers (url/title/relevance), metadata only.
- `claims`           — the claim_provenance grain: ONE fact + its source +
                       confidence + verbatim quote. This is the traceability gate.
- `identity_candidates` — EVERY source the identity gate judged, with its
                       confidence, decision, and reason trail, so a wrong merge
                       is auditable and recoverable (we never silently drop a
                       candidate).
- `batch_status`     — per-(person, phase) resume state for multi-hour runs.

Writes are idempotent: re-enriching a person replaces that person's rows for
the affected phase rather than duplicating them.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass

# Phases tracked in batch_status. Kept here so callers don't pass free strings.
PHASE_DISCOVERY = "discovery"
PHASE_IDENTITY = "identity"
PHASE_STRUCTURING = "structuring"

# Merge-gate outcomes recorded on every candidate (never just the winner).
DECISION_ACCEPT = "auto_accept"
DECISION_REVIEW = "review"
DECISION_REJECT = "reject"

_ENRICHMENT_SCHEMA = """
CREATE TABLE IF NOT EXISTS person_sources (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    url          TEXT    NOT NULL,
    domain       TEXT    NOT NULL,
    title        TEXT    NOT NULL DEFAULT '',
    relevance    REAL    NOT NULL DEFAULT 0.0,
    fetched_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, url)
);
CREATE INDEX IF NOT EXISTS idx_sources_person ON person_sources (person_id);

CREATE TABLE IF NOT EXISTS claims (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id         INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    claim_type        TEXT    NOT NULL,
    value             TEXT    NOT NULL,
    source_url        TEXT    NOT NULL,
    quote             TEXT    NOT NULL DEFAULT '',
    confidence        REAL    NOT NULL,
    extraction_method TEXT    NOT NULL,
    extraction_date   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_claims_person ON claims (person_id);
CREATE INDEX IF NOT EXISTS idx_claims_type   ON claims (person_id, claim_type);

CREATE TABLE IF NOT EXISTS identity_candidates (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id    INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    source_url   TEXT    NOT NULL,
    confidence   REAL    NOT NULL,
    decision     TEXT    NOT NULL,
    reason       TEXT    NOT NULL DEFAULT '',
    model        TEXT    NOT NULL,
    evaluated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (person_id, source_url)
);
CREATE INDEX IF NOT EXISTS idx_candidates_person   ON identity_candidates (person_id);
CREATE INDEX IF NOT EXISTS idx_candidates_decision ON identity_candidates (decision);

CREATE TABLE IF NOT EXISTS batch_status (
    person_id   INTEGER NOT NULL REFERENCES people(id) ON DELETE CASCADE,
    phase       TEXT    NOT NULL,
    status      TEXT    NOT NULL,
    retry_count INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT,
    updated_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (person_id, phase)
);
"""


@dataclass(frozen=True)
class ClaimRow:
    """One persisted claim. value/quote are plain text; the source_url +
    quote together make the claim independently verifiable by a human."""

    claim_type: str
    value: str
    source_url: str
    quote: str
    confidence: float
    extraction_method: str


@dataclass(frozen=True)
class SourceRow:
    url: str
    domain: str
    title: str
    relevance: float


@dataclass(frozen=True)
class CandidateRow:
    """One identity-gate verdict for one source. Stored whether accepted,
    queued for review, or rejected — the reason trail is the audit record."""

    source_url: str
    confidence: float
    decision: str
    reason: str
    model: str


def init_enrichment_schema(conn: sqlite3.Connection) -> None:
    """Create the Stage-2 tables. Safe to call repeatedly; leaves the Stage-1
    `people` table untouched (that is owned by db.init_schema)."""
    conn.executescript(_ENRICHMENT_SCHEMA)


def replace_sources(
    conn: sqlite3.Connection, person_id: int, sources: Sequence[SourceRow]
) -> int:
    """Idempotent: drop this person's prior source rows, insert the new set."""
    conn.execute("DELETE FROM person_sources WHERE person_id = ?", (person_id,))
    rows = [
        {
            "person_id": person_id,
            "url": s.url,
            "domain": s.domain,
            "title": s.title,
            "relevance": s.relevance,
        }
        for s in sources
    ]
    conn.executemany(
        "INSERT INTO person_sources (person_id, url, domain, title, relevance) "
        "VALUES (:person_id, :url, :domain, :title, :relevance)",
        rows,
    )
    return len(rows)


def replace_candidates(
    conn: sqlite3.Connection, person_id: int, candidates: Sequence[CandidateRow]
) -> int:
    """Idempotent: replace this person's identity-candidate trail."""
    conn.execute(
        "DELETE FROM identity_candidates WHERE person_id = ?", (person_id,)
    )
    rows = [
        {
            "person_id": person_id,
            "source_url": c.source_url,
            "confidence": c.confidence,
            "decision": c.decision,
            "reason": c.reason,
            "model": c.model,
        }
        for c in candidates
    ]
    conn.executemany(
        "INSERT INTO identity_candidates "
        "(person_id, source_url, confidence, decision, reason, model) "
        "VALUES (:person_id, :source_url, :confidence, :decision, :reason, :model)",
        rows,
    )
    return len(rows)


def replace_claims(
    conn: sqlite3.Connection, person_id: int, claims: Sequence[ClaimRow]
) -> int:
    """Idempotent: replace this person's claims with the freshly extracted set.
    Re-enrichment over time supersedes rather than accumulates duplicates."""
    conn.execute("DELETE FROM claims WHERE person_id = ?", (person_id,))
    rows = [
        {
            "person_id": person_id,
            "claim_type": c.claim_type,
            "value": c.value,
            "source_url": c.source_url,
            "quote": c.quote,
            "confidence": c.confidence,
            "extraction_method": c.extraction_method,
        }
        for c in claims
    ]
    conn.executemany(
        "INSERT INTO claims "
        "(person_id, claim_type, value, source_url, quote, confidence, extraction_method) "
        "VALUES (:person_id, :claim_type, :value, :source_url, :quote, :confidence, "
        ":extraction_method)",
        rows,
    )
    return len(rows)


def mark_phase(
    conn: sqlite3.Connection,
    person_id: int,
    phase: str,
    status: str,
    *,
    last_error: str | None = None,
    increment_retry: bool = False,
) -> None:
    """Upsert resume state for (person, phase). increment_retry bumps the count
    on a retry; a clean run leaves it at its prior value."""
    bump = "retry_count + 1" if increment_retry else "retry_count"
    conn.execute(
        f"""
        INSERT INTO batch_status (person_id, phase, status, retry_count, last_error)
        VALUES (:person_id, :phase, :status, 0, :last_error)
        ON CONFLICT (person_id, phase) DO UPDATE SET
            status      = excluded.status,
            retry_count = {bump},
            last_error  = excluded.last_error,
            updated_at  = datetime('now')
        """,
        {
            "person_id": person_id,
            "phase": phase,
            "status": status,
            "last_error": last_error,
        },
    )


def pending_people(
    conn: sqlite3.Connection, phase: str, limit: int
) -> list[int]:
    """Person ids that have NOT completed `phase` (no row, or status != 'done').
    Drives resumable batches: a re-run only picks up unfinished work."""
    rows = conn.execute(
        """
        SELECT p.id
        FROM people p
        LEFT JOIN batch_status b
               ON b.person_id = p.id AND b.phase = ?
        WHERE p.needs_review = 0
          AND (b.status IS NULL OR b.status != 'done')
        ORDER BY p.id
        LIMIT ?
        """,
        (phase, limit),
    ).fetchall()
    return [r["id"] for r in rows]
