"""Recompute sector classifications over the existing person_insights rows.

phase2 stores deterministic sectors (PDL industry -> employer-name keyword) as it
enriches. This batch does two things, WITHOUT re-enriching anyone:

1. Reflows EVERY person's `current_sector` and `first_sector` under the current
   taxonomy — so adding a sector or a keyword to `sector_classify.py` updates the
   whole cohort in one cheap pass.
2. Upgrades the AMBIGUOUS catch-all remainder with ONE Haiku call (employer +
   title + industry — richer than the keyword classifier, which ignores title),
   constrained to the fixed taxonomy. People the deterministic classifier already
   placed are left as-is; only the catch-all is sent to the model.

Read-mostly: only the two sector columns are UPDATEd. The DB is backed up first.

    python reclassify_sectors.py            # deterministic + Haiku upgrade
    python reclassify_sectors.py --no-llm   # deterministic reflow only (free)
    python reclassify_sectors.py --dry-run  # report, write nothing
"""
from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter

from anthropic import Anthropic

from config import DB_PATH, require_key
from cost_log import append_entry, build_entry
from db import connect, init_schema
from insights_llm import classify_sectors
from insights_store import init_insights_schema
from person_insights_store import init_person_insights_schema
from sector_classify import SECTOR_CATCHALL, classify_sector

# (employer, title, industry) — the key we de-dup distinct classification
# contexts by, so the Haiku call stays small no matter the cohort size.
_Context = tuple[str, str, str]


def _load_rows(conn) -> list[dict]:
    """Per enriched person: first employer, current industry, and the current
    employer / title from their claims (one each)."""
    cur = conn.execute(
        """
        SELECT
          pi.person_id                              AS person_id,
          COALESCE(pi.first_employer, '')           AS first_employer,
          COALESCE(pi.current_industry, '')         AS industry,
          (SELECT c.value FROM claims c
             WHERE c.person_id = pi.person_id
               AND c.claim_type = 'current_employer' LIMIT 1) AS cur_emp,
          (SELECT c.value FROM claims c
             WHERE c.person_id = pi.person_id
               AND c.claim_type = 'current_title' LIMIT 1)    AS cur_title
        FROM person_insights pi
        """
    )
    return [dict(r) for r in cur.fetchall()]


def _deterministic(rows: list[dict]) -> dict[int, tuple[str, str]]:
    """person_id -> (current_sector, first_sector) from the pure classifier."""
    out: dict[int, tuple[str, str]] = {}
    for r in rows:
        cur = classify_sector(r["cur_emp"] or "", r["industry"])
        first = classify_sector(r["first_employer"])
        out[r["person_id"]] = (cur, first)
    return out


def _ambiguous_contexts(rows: list[dict], det: dict[int, tuple[str, str]]) -> list[_Context]:
    """Distinct (employer, title, industry) contexts the deterministic pass left
    in the catch-all — current and first employers both."""
    contexts: set[_Context] = set()
    for r in rows:
        cur_sector, first_sector = det[r["person_id"]]
        if cur_sector == SECTOR_CATCHALL and (r["cur_emp"] or "").strip():
            contexts.add(((r["cur_emp"] or ""), (r["cur_title"] or ""), r["industry"]))
        if first_sector == SECTOR_CATCHALL and (r["first_employer"] or "").strip():
            contexts.add(((r["first_employer"] or ""), "", ""))
    return sorted(contexts)


def run(*, use_llm: bool, dry_run: bool) -> int:
    if not dry_run:
        backup = f"{DB_PATH}.bak-before-reclassify"
        shutil.copy2(DB_PATH, backup)
        print(f"Backed up DB -> {backup}")

    with connect(DB_PATH) as conn:
        init_schema(conn)
        init_insights_schema(conn)
        init_person_insights_schema(conn)  # applies the first_sector migration

        rows = _load_rows(conn)
        if not rows:
            print("No enriched people to reclassify.", file=sys.stderr)
            return 1

        det = _deterministic(rows)

        # Haiku upgrade of the ambiguous remainder (optional).
        upgrade: dict[_Context, str] = {}
        tin = tout = 0
        contexts = _ambiguous_contexts(rows, det) if use_llm else []
        if contexts:
            anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
            result = classify_sectors(anthropic, contexts)
            upgrade = dict(zip(contexts, result.labels))
            tin, tout = result.input_tokens, result.output_tokens

        def resolve(det_sector: str, ctx: _Context) -> str:
            if det_sector != SECTOR_CATCHALL:
                return det_sector
            return upgrade.get(ctx, det_sector)

        final: dict[int, tuple[str, str]] = {}
        for r in rows:
            pid = r["person_id"]
            cur_det, first_det = det[pid]
            cur = resolve(cur_det, ((r["cur_emp"] or ""), (r["cur_title"] or ""), r["industry"]))
            first = resolve(first_det, ((r["first_employer"] or ""), "", ""))
            final[pid] = (cur, first)

        before = Counter(s for s, _ in det.values())
        after = Counter(c for c, _ in final.values())
        print(f"\nCurrent-sector distribution ({len(rows)} people):")
        for sector in sorted(set(before) | set(after), key=lambda s: -after.get(s, 0)):
            print(f"  {after.get(sector, 0):>3}  {sector}   (was {before.get(sector, 0)})")
        moved = sum(1 for pid in final if final[pid][0] != det[pid][0])
        print(f"\nHaiku reassigned {moved} of {before.get(SECTOR_CATCHALL, 0)} catch-all people.")

        if dry_run:
            print("\n--dry-run: no writes.")
            return 0

        for pid, (cur, first) in final.items():
            conn.execute(
                "UPDATE person_insights SET current_sector = ?, first_sector = ? WHERE person_id = ?",
                (cur, first, pid),
            )
        conn.commit()
        print(f"\nUpdated current_sector + first_sector for {len(final)} people.")

    if use_llm and (tin or tout):
        entry = build_entry(label="reclassify-sectors", people=len(rows), haiku_in=tin, haiku_out=tout)
        append_entry(entry)
        print(f"Haiku reclassify cost: ${entry.total_usd:.4f} -> data/cost_log.jsonl")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reclassify-sectors", description=__doc__)
    p.add_argument("--no-llm", action="store_true", help="Deterministic reflow only (no Haiku, free)")
    p.add_argument("--dry-run", action="store_true", help="Report the new distribution, write nothing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(use_llm=not args.no_llm, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
