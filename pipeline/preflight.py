"""Pre-flight go/no-go check + post-run "couldn't enrich" report for an
enrichment run. Read-only: spends nothing, changes nothing.

    python preflight.py            # pre-flight checks for a --rerun-enriched base sweep
    python preflight.py --report   # who couldn't be enriched after a run

Pre-flight verifies the things that silently ruin a run: missing keys, no DB
backup, an empty target set, and shows the Firecrawl balance + a rough cost.
Exit code 0 = GO, 1 = NO-GO (something a human must fix first).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from config import DB_PATH
from cost_log import PDL_USD_PER_MATCH, remaining_credits
from db import connect

# Rough per-person cost of a base sweep (PDL match + Perplexity search/mentions +
# Anthropic identity/structure/bio). Firecrawl is 0 at --max-credits 0. Empirical
# from the canary: ~$0.39/person.
BASE_SWEEP_USD_PER_PERSON = 0.40


def _keys() -> list[tuple[str, bool, bool]]:
    """(name, present, required) — required keys block the run; soft keys degrade it."""
    return [
        ("ANTHROPIC_API_KEY", bool(os.getenv("ANTHROPIC_API_KEY")), True),
        ("FIRECRAWL_API_KEY", bool(os.getenv("FIRECRAWL_API_KEY")), True),  # client ctor
        ("PDL_API_KEY", bool(os.getenv("PDL_API_KEY")), False),
        ("PERPLEXITY_API_KEY", bool(os.getenv("PERPLEXITY_API_KEY")), False),
    ]


def _latest_backup() -> Path | None:
    backups = sorted(Path(DB_PATH).parent.glob("titans.backup.*.db"))
    return backups[-1] if backups else None


def _rerun_target_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM people p JOIN person_insights pi ON pi.person_id = p.id"
    ).fetchone()[0]


def preflight() -> int:
    print("\n=== PRE-FLIGHT: --rerun-enriched base sweep ===\n")
    ok = True

    print("API keys:")
    for name, present, required in _keys():
        tag = "REQUIRED" if required else "soft"
        mark = "✓" if present else ("✗ MISSING" if required else "— (degrades)")
        print(f"  {mark:<14} {name} [{tag}]")
        if required and not present:
            ok = False

    backup = _latest_backup()
    print("\nDB backup:")
    if backup:
        print(f"  ✓ latest: {backup.name}")
    else:
        print("  ✗ NO backup found — make one before a rerun "
              "(cp data/titans.db data/titans.backup.<date>-<label>.db)")
        ok = False

    # Firecrawl balance is informational for a base sweep (--max-credits 0 spends
    # none), but a NO-GO for a deep pass.
    print("\nFirecrawl balance:")
    try:
        from firecrawl import Firecrawl
        fc = Firecrawl(api_key=os.getenv("FIRECRAWL_API_KEY"))
        print(f"  {remaining_credits(fc)} credits (base sweep spends 0; matters for the deep pass)")
    except Exception as exc:  # noqa: BLE001
        print(f"  — could not read ({exc})")

    with connect(Path(DB_PATH)) as conn:
        n = _rerun_target_count(conn)
    print("\nTargets:")
    print(f"  {n} already-enriched people (--rerun-enriched)")
    print(f"  est. cost ≈ ${n * BASE_SWEEP_USD_PER_PERSON:,.2f} "
          f"(~${BASE_SWEEP_USD_PER_PERSON:.2f}/person, Firecrawl $0)")
    if n == 0:
        print("  ✗ nothing to rerun")
        ok = False

    print(f"\n=== {'GO' if ok else 'NO-GO — fix the ✗ items above'} ===\n")
    return 0 if ok else 1


def report() -> int:
    """Who couldn't be enriched: errored, still-thin (flagged), or zero-claim."""
    with connect(Path(DB_PATH)) as conn:
        errored = conn.execute(
            "SELECT b.person_id, p.full_name, b.last_error FROM batch_status b "
            "JOIN people p ON p.id = b.person_id "
            "WHERE b.phase='structuring' AND b.status='error' ORDER BY b.person_id"
        ).fetchall()
        thin = conn.execute(
            "SELECT pi.person_id, p.full_name, pi.completeness_score, pi.deep_search_reason "
            "FROM person_insights pi JOIN people p ON p.id = pi.person_id "
            "WHERE pi.needs_deep_search=1 ORDER BY pi.completeness_score, pi.person_id"
        ).fetchall()
        zero = conn.execute(
            "SELECT p.id, p.full_name FROM people p "
            "JOIN person_insights pi ON pi.person_id = p.id "
            "WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.person_id = p.id) "
            "ORDER BY p.id"
        ).fetchall()

    print(f"\n=== COULDN'T FULLY ENRICH ===\n")
    print(f"ERRORED ({len(errored)}) — rolled back, safe to re-run:")
    for r in errored:
        print(f"  [{r['person_id']:>4}] {r['full_name']:<26} {(r['last_error'] or '')[:60]}")
    print(f"\nZERO CLAIMS ({len(zero)}) — no data found (likely genuine ghosts):")
    for r in zero:
        print(f"  [{r['id']:>4}] {r['full_name']}")
    print(f"\nTHIN / FLAGGED FOR DEEP ({len(thin)}) — enriched but incomplete:")
    for r in thin:
        print(f"  [{r['person_id']:>4}] {r['completeness_score']:>3}  "
              f"{r['full_name']:<26} {r['deep_search_reason']}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="post-run report of who couldn't be enriched")
    args = ap.parse_args(argv)
    return report() if args.report else preflight()


if __name__ == "__main__":
    sys.exit(main())
