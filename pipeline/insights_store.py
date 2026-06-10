"""SQLite persistence for the aggregate "insights" layer (Phase 3).

Where Stage 2 stores ONE fact per person (the `claims` grain), this stores ONE
roll-up of the WHOLE cohort per year — the numbers and prose that drive the web
app's "Overview & Insights" view. Keyed by year so the pass can be re-run
annually and year-over-year deltas fall out of a simple cross-year query.

A snapshot row carries:
- the deterministic roll-ups (landing firms, current titles, seniority ladder)
  as a JSON payload, computed by SQL GROUP BY — cheap, no model,
- a short narrative (the only genuinely model-written field),
- coverage bookkeeping (how many of the cohort are actually enriched yet) and an
  `is_sample` flag the web reads verbatim: while coverage is below threshold the
  real numbers are too sparse to publish, so the web keeps showing its seeded
  illustration unchanged. The flag flips to real automatically the run AFTER
  enrichment coverage crosses COVERAGE_THRESHOLD — no web/UI change required.

Writes are idempotent on snapshot_year: re-running a year replaces that year's
row rather than duplicating it. The pipeline owns these writes; the web app
opens the same file READ-ONLY.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field

# A snapshot is only published as "real" once enough of the cohort is enriched
# that the aggregate numbers mean something. Below this, the web keeps showing
# its seeded illustration (is_sample = 1) so the demo never lies about coverage.
COVERAGE_THRESHOLD = 0.5  # fraction of the cohort that must be enriched
MIN_ENRICHED_FOR_REAL = 50  # absolute floor, so a tiny cohort can't trip % alone

# The fixed seniority ladder. The model maps titles ONTO these buckets; it never
# invents a new tier. Order is career order, shallow→senior, so the web renders
# the ladder top-to-bottom without re-sorting.
SENIORITY_TIERS = (
    "Analyst / Associate",
    "VP / Principal",
    "Director / Managing Director",
    "Partner / Founder",
    "C-suite / Owner",
)
SENIORITY_UNKNOWN = "Unknown"

_INSIGHTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS insights_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_year    INTEGER NOT NULL,
    generated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    people_total     INTEGER NOT NULL,
    enriched_count   INTEGER NOT NULL,
    coverage         REAL    NOT NULL,
    is_sample        INTEGER NOT NULL,
    narrative        TEXT    NOT NULL DEFAULT '',
    payload          TEXT    NOT NULL,
    haiku_tokens_in  INTEGER NOT NULL DEFAULT 0,
    haiku_tokens_out INTEGER NOT NULL DEFAULT 0,
    UNIQUE (snapshot_year)
);
CREATE INDEX IF NOT EXISTS idx_snapshot_year ON insights_snapshot (snapshot_year);
"""


@dataclass(frozen=True)
class FirmCount:
    company: str
    count: int


@dataclass(frozen=True)
class TitleCount:
    title: str
    count: int


@dataclass(frozen=True)
class SeniorityTier:
    tier: str
    count: int


@dataclass(frozen=True)
class SignatureStat:
    label: str
    value: str
    detail: str
    pct: int
    # Stable identifier for the metric (e.g. "buy_side", "reached_md"), so the web
    # can wire each scorecard tile to its people drill-down without matching the
    # display label. Empty on pre-key snapshots (the tile is then non-clickable).
    key: str = ""


@dataclass(frozen=True)
class SectorCount:
    sector: str
    count: int


@dataclass(frozen=True)
class InsightsSnapshot:
    """The full per-year roll-up. The deterministic fields are measured by SQL;
    `narrative` is the only model-written field. `is_sample` is computed from
    coverage and is what the web trusts when deciding real-vs-illustrative."""

    snapshot_year: int
    people_total: int
    enriched_count: int
    coverage: float
    is_sample: bool
    narrative: str
    landing_firms: tuple[FirmCount, ...] = ()
    current_titles: tuple[TitleCount, ...] = ()
    seniority: tuple[SeniorityTier, ...] = ()
    signature_stats: tuple[SignatureStat, ...] = ()
    landing_sectors: tuple[SectorCount, ...] = ()
    founders_partners: int = 0
    haiku_tokens_in: int = 0
    haiku_tokens_out: int = 0


def is_sample_for(enriched_count: int, people_total: int) -> bool:
    """Single source of truth for real-vs-illustrative. True (still seeded) until
    coverage clears BOTH the percentage threshold and the absolute floor."""
    if people_total <= 0:
        return True
    coverage = enriched_count / people_total
    return enriched_count < MIN_ENRICHED_FOR_REAL or coverage < COVERAGE_THRESHOLD


def init_insights_schema(conn: sqlite3.Connection) -> None:
    """Create the Phase-3 table. Safe to call repeatedly; leaves Stage-1/2
    tables untouched."""
    conn.executescript(_INSIGHTS_SCHEMA)


def _payload_dict(snap: InsightsSnapshot) -> dict:
    """The non-scalar roll-ups, serialized into the single payload column. Kept
    as a JSON blob (not extra tables) because the web reads the whole snapshot at
    once and never queries inside it."""
    return {
        "landing_firms": [{"company": f.company, "count": f.count} for f in snap.landing_firms],
        "current_titles": [{"title": t.title, "count": t.count} for t in snap.current_titles],
        "seniority": [{"tier": s.tier, "count": s.count} for s in snap.seniority],
        "signature_stats": [
            {"label": s.label, "value": s.value, "detail": s.detail, "pct": s.pct, "key": s.key}
            for s in snap.signature_stats
        ],
        "landing_sectors": [
            {"sector": s.sector, "count": s.count} for s in snap.landing_sectors
        ],
        "founders_partners": snap.founders_partners,
    }


def replace_snapshot(conn: sqlite3.Connection, snap: InsightsSnapshot) -> None:
    """Idempotent on snapshot_year: re-running a year supersedes its prior row.
    Year-over-year history is preserved across DISTINCT years, not within one."""
    conn.execute(
        """
        INSERT INTO insights_snapshot (
            snapshot_year, people_total, enriched_count, coverage, is_sample,
            narrative, payload, haiku_tokens_in, haiku_tokens_out
        ) VALUES (
            :year, :total, :enriched, :coverage, :is_sample,
            :narrative, :payload, :hin, :hout
        )
        ON CONFLICT (snapshot_year) DO UPDATE SET
            generated_at     = datetime('now'),
            people_total     = excluded.people_total,
            enriched_count   = excluded.enriched_count,
            coverage         = excluded.coverage,
            is_sample        = excluded.is_sample,
            narrative        = excluded.narrative,
            payload          = excluded.payload,
            haiku_tokens_in  = excluded.haiku_tokens_in,
            haiku_tokens_out = excluded.haiku_tokens_out
        """,
        {
            "year": snap.snapshot_year,
            "total": snap.people_total,
            "enriched": snap.enriched_count,
            "coverage": snap.coverage,
            "is_sample": 1 if snap.is_sample else 0,
            "narrative": snap.narrative,
            "payload": json.dumps(_payload_dict(snap)),
            "hin": snap.haiku_tokens_in,
            "hout": snap.haiku_tokens_out,
        },
    )


def latest_snapshot(conn: sqlite3.Connection) -> InsightsSnapshot | None:
    """The most recent year's snapshot, or None if no pass has run yet. This is
    what the web reads; absence simply means the web keeps its seeded view."""
    row = conn.execute(
        "SELECT * FROM insights_snapshot ORDER BY snapshot_year DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    payload = json.loads(row["payload"])
    return InsightsSnapshot(
        snapshot_year=row["snapshot_year"],
        people_total=row["people_total"],
        enriched_count=row["enriched_count"],
        coverage=row["coverage"],
        is_sample=bool(row["is_sample"]),
        narrative=row["narrative"],
        landing_firms=tuple(
            FirmCount(f["company"], f["count"]) for f in payload.get("landing_firms", [])
        ),
        current_titles=tuple(
            TitleCount(t["title"], t["count"]) for t in payload.get("current_titles", [])
        ),
        seniority=tuple(
            SeniorityTier(s["tier"], s["count"]) for s in payload.get("seniority", [])
        ),
        signature_stats=tuple(
            SignatureStat(
                label=s["label"],
                value=s["value"],
                detail=s["detail"],
                pct=s["pct"],
                key=s.get("key", ""),
            )
            for s in payload.get("signature_stats", [])
        ),
        landing_sectors=tuple(
            SectorCount(s["sector"], s["count"])
            for s in payload.get("landing_sectors", [])
        ),
        founders_partners=payload.get("founders_partners", 0),
        haiku_tokens_in=row["haiku_tokens_in"],
        haiku_tokens_out=row["haiku_tokens_out"],
    )
