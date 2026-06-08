"""One-off: apply the LLM reconciliation pass to already-enriched people.

Lets you SEE the reconciler's effect on real multi-source profiles without
re-charging PDL/Perplexity — it only re-reads the claims already in the DB,
reconciles them (one Haiku call/person, ~$0.004), and rewrites the clean set.

    python reconcile_existing.py --dry-run     # preview before/after, write nothing
    python reconcile_existing.py               # apply + persist
    python reconcile_existing.py --name "Jason Kaspar"

Like renormalize_claims.py this is a backfill convenience; fresh ingests already
reconcile inline (phase2_enrich.py). Safe to re-run — reconciliation converges.
"""
from __future__ import annotations

import argparse
import sys

from anthropic import Anthropic

from config import DB_PATH, require_key
from db import connect
from enrichment_store import ClaimRow, replace_claims
from normalize import digest_claims
from reconcile import _RECONCILE_TYPES, reconcile_claims


def _load_people(conn, name: str | None) -> list[dict]:
    sql = """
        SELECT p.id, p.full_name
        FROM people p
        JOIN batch_status b ON b.person_id = p.id
          AND b.phase = 'structuring' AND b.status = 'done'
    """
    if name:
        rows = conn.execute(sql + " WHERE p.full_name = ?", (name,)).fetchall()
    else:
        rows = conn.execute(sql + " ORDER BY p.id").fetchall()
    return [dict(r) for r in rows]


def _load_claims(conn, pid: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ?",
        (pid,),
    ).fetchall()
    return [
        ClaimRow(
            claim_type=r["claim_type"],
            value=r["value"],
            source_url=r["source_url"],
            quote=r["quote"] or "",
            confidence=r["confidence"],
            extraction_method=r["extraction_method"],
        )
        for r in rows
    ]


def _resume_values(claims: list[ClaimRow]) -> list[str]:
    return sorted(
        f"{c.claim_type}: {c.value}" for c in claims if c.claim_type in _RECONCILE_TYPES
    )


def run(name: str | None, dry_run: bool) -> int:
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
    with connect(DB_PATH) as conn:
        people = _load_people(conn, name)
        if not people:
            print("No enriched people found.", file=sys.stderr)
            return 1

        mode = "DRY RUN — nothing will be written" if dry_run else "APPLYING"
        print(f"Reconciling {len(people)} enriched people  [{mode}]\n")

        total_before = total_after = 0
        for p in people:
            pid, full_name = p["id"], p["full_name"]
            before = _load_claims(conn, pid)
            reconciled, _, _ = reconcile_claims(anthropic, full_name, before)
            after = digest_claims(reconciled)

            b_resume, a_resume = _resume_values(before), _resume_values(after)
            total_before += len(b_resume)
            total_after += len(a_resume)
            delta = len(a_resume) - len(b_resume)
            print(f"=== {full_name} ===  résumé facts {len(b_resume)} → {len(a_resume)} ({delta:+d})")

            removed = [v for v in b_resume if v not in a_resume]
            added = [v for v in a_resume if v not in b_resume]
            for v in removed:
                print(f"    -  {v}")
            for v in added:
                print(f"    +  {v}")
            if not removed and not added:
                print("    (no résumé changes)")
            print()

            if not dry_run:
                replace_claims(conn, pid, after)
                conn.commit()

        print("─" * 60)
        print(f"TOTAL résumé facts: {total_before} → {total_after} "
              f"({total_after - total_before:+d})")
        if dry_run:
            print("\nDry run — re-run without --dry-run to persist.")
        else:
            print("\nApplied. Run `cd web && npm run sync-db` to update the site.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reconcile-existing", description=__doc__)
    p.add_argument("--name", default=None, help="One specific person by full name")
    p.add_argument("--dry-run", action="store_true", help="Preview without writing")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run(name=args.name, dry_run=args.dry_run)


if __name__ == "__main__":
    import config  # noqa: F401  (loads .env)
    sys.exit(main())
