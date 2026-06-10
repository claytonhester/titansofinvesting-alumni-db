#!/usr/bin/env bash
# Post-enrichment finalize — run this after ANY phase2_enrich batch (run_class12.sh
# calls it automatically). phase2 writes per-person data and deterministic sectors,
# but the BATCH-level steps below are what make the Overview match what we ship:
#
#   1. reclassify_sectors.py   — Haiku upgrade of the ambiguous "Other / Operating"
#                                remainder + reflow current_sector/first_sector under
#                                the current taxonomy (no re-enrichment).
#   2. phase3_insights.py --llm — cohort snapshot WITH the billed Haiku overlay:
#                                canonicalized + seniority-ordered current titles,
#                                seniority ladder, and the narrative. MUST be --llm,
#                                or the snapshot reverts to the templated narrative
#                                and raw (un-canonicalized) titles.
#   3. npm run embed           — rebuild person_vectors (semantic search) in the
#                                pipeline DB so new/changed profiles are findable.
#   4. npm run sync-db         — copy pipeline DB -> web/data/titans.db (the tracked,
#                                deployed snapshot). Commit the web DB to ship.
#
# Idempotent and safe to re-run. Each step is independent; a failure is reported but
# does not abort the rest, so a missing key on one step still lets the others run.
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

step() {
  echo ""
  echo "----- $1 -----"
  shift
  if "$@"; then
    echo "  ok"
  else
    echo "  !! step failed (rc=$?) — continuing"
  fi
}

echo "===== FINALIZE PASS — START $(date) ====="

step "1/4 reclassify sectors (Haiku catch-all upgrade)" \
  python -u reclassify_sectors.py

step "2/4 phase3 insights snapshot (--llm: titles + seniority + narrative)" \
  python -u phase3_insights.py --llm

step "3/4 re-embed (semantic search vectors)" \
  npm --prefix ../web run embed

step "4/4 sync pipeline DB -> web snapshot" \
  npm --prefix ../web run sync-db

echo ""
echo "===== FINALIZE PASS — DONE $(date) ====="
echo "Remember to commit web/data/titans.db to ship the refreshed snapshot."
