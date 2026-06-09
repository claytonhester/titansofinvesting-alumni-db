"""Phase 3 insights orchestrator: cohort -> one per-year snapshot.

Assembles the aggregate "state of the cohort" view that drives the web app's
Overview & Insights page. Designed to be re-run every year (and re-run within a
year as enrichment deepens), keyed by snapshot_year so year-over-year deltas
fall out of a cross-year query.

Two cost tiers, opt-in:

    build_snapshot()      deterministic SQL GROUP BY roll-ups        FREE
    --llm overlay         two Haiku calls (seniority + narrative)    BILLED

The default run is FREE: it measures everything by SQL and writes a templated
narrative. `--llm` is opt-in and adds the two billed Haiku calls, logged to
data/cost_log.jsonl like every other spend. The is_sample flag is decided from
REAL coverage either way, so the web's real-vs-illustrative gate stays honest
no matter how the snapshot was produced.

    python phase3_insights.py                 # free, current year
    python phase3_insights.py --year 2026      # free, explicit year
    python phase3_insights.py --llm            # billed Haiku overlay

The web opens the same DB READ-ONLY; this orchestrator owns the writes.
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone

from anthropic import Anthropic

from config import DB_PATH, require_key
from cost_log import append_entry, build_entry
from db import connect, init_schema
from insights_llm import classify_seniority, write_narrative
from insights_rollup import (
    _value_counts,
    build_snapshot,
    landing_firms,
    with_llm_narrative,
)
from insights_store import (
    InsightsSnapshot,
    init_insights_schema,
    replace_snapshot,
)
from kpi_rollup import with_kpi_stats
from person_insights_store import (
    PersonInsight,
    all_person_insights,
    init_person_insights_schema,
)


def _apply_llm_overlay(
    conn,
    snap: InsightsSnapshot,
) -> InsightsSnapshot:
    """Run the two billed Haiku calls and overlay their outputs onto the
    deterministic snapshot. Seniority is reclassified over the full title
    vocabulary; the narrative is rewritten over the SAME pre-computed numbers
    (including the four headline KPIs already on the snapshot). Token counts ride
    along on the returned snapshot for cost logging."""
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))

    title_counts = _value_counts(conn, "current_title")
    firms = landing_firms(conn)
    distinct_employers = len(_value_counts(conn, "current_employer"))

    seniority_cls = classify_seniority(anthropic, title_counts)
    new_seniority = seniority_cls.tiers or snap.seniority

    narrative = write_narrative(
        anthropic,
        people=snap.people_total,
        enriched=snap.enriched_count,
        firms=firms,
        distinct_employers=distinct_employers,
        founders_partners=snap.founders_partners,
        seniority=new_seniority,
        kpis=snap.signature_stats,
    )

    overlaid = with_llm_narrative(
        snap,
        narrative=narrative.text,
        seniority=new_seniority,
        haiku_tokens_in=seniority_cls.input_tokens + narrative.input_tokens,
        haiku_tokens_out=seniority_cls.output_tokens + narrative.output_tokens,
    )
    return overlaid


def run(year: int | None, use_llm: bool) -> int:
    snapshot_year = year if year is not None else datetime.now(timezone.utc).year

    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_insights_schema(conn)
        init_person_insights_schema(conn)

        snap = build_snapshot(conn, snapshot_year)
        if snap.people_total == 0:
            print("No people in cohort — nothing to snapshot.", file=sys.stderr)
            return 1

        # Overlay the four per-person KPIs as the scorecard (empty until people
        # are classified, so the web shows an empty state rather than zeros).
        insights = all_person_insights(conn)
        snap = with_kpi_stats(snap, insights, snapshot_year=snapshot_year)

        if use_llm:
            snap = _apply_llm_overlay(conn, snap)

        replace_snapshot(conn, snap)
        conn.commit()

    if use_llm and (snap.haiku_tokens_in or snap.haiku_tokens_out):
        entry = build_entry(
            label=f"insights-{snapshot_year}",
            people=snap.enriched_count,
            haiku_in=snap.haiku_tokens_in,
            haiku_out=snap.haiku_tokens_out,
        )
        append_entry(entry)
        print(f"LLM overlay cost: ${entry.total_usd:.4f} -> data/cost_log.jsonl")

    flag = "illustrative (is_sample)" if snap.is_sample else "REAL"
    print(
        f"Snapshot {snapshot_year}: {snap.enriched_count}/{snap.people_total} "
        f"enriched, coverage {snap.coverage:.1%} -> {flag}. "
        f"{len(snap.landing_firms)} firms, {len(snap.seniority)} tiers, "
        f"{snap.founders_partners} senior."
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phase3-insights", description=__doc__)
    p.add_argument(
        "--year",
        type=int,
        default=None,
        help="Snapshot year (default: current UTC year)",
    )
    p.add_argument(
        "--llm",
        action="store_true",
        help="Add the billed Haiku seniority + narrative overlay (default: free)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(year=args.year, use_llm=args.llm)


if __name__ == "__main__":
    sys.exit(main())
