#!/usr/bin/env bash
# Post-enrichment finalize — run this after ANY phase2_enrich batch (run_class12.sh
# calls it automatically). phase2 writes per-person data and deterministic sectors,
# but the BATCH-level steps below are what make the Overview match what we ship:
#
#   1. reclassify_sectors.py   — Haiku upgrade of the ambiguous "Other / Operating"
#                                remainder + reflow current_sector/first_sector under
#                                the current taxonomy (no re-enrichment).
#   2. compute_completeness.py — free deterministic 0-100 profile-quality score per
#                                person (Build Status surfaces avg/low + refresh
#                                candidates so weak profiles raise their own hand).
#   3. reclassify_levels.py    — cross-industry seniority ladder: classify every
#                                role (cached, ~pennies), write peak_level + the two
#                                thresholds (Senior Leadership / Manager) + the
#                                career trajectory. Runs BEFORE phase3 so the KPI
#                                rollup reads the fresh columns.
#   4. phase3_insights.py --llm — cohort snapshot WITH the billed Haiku overlay:
#                                canonicalized + seniority-ordered current titles,
#                                seniority ladder, and the narrative. MUST be --llm,
#                                or the snapshot reverts to the templated narrative
#                                and raw (un-canonicalized) titles.
#   5. npm run embed           — rebuild person_vectors (semantic search) in the
#                                pipeline DB so new/changed profiles are findable.
#   6. npm run sync-db         — copy pipeline DB -> web/data/titans.db (the tracked,
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

step "1/6 reclassify sectors (Haiku catch-all upgrade)" \
  python -u reclassify_sectors.py

step "2/6 profile completeness scores (free, deterministic)" \
  python -u compute_completeness.py

step "3/6 reclassify seniority levels (cross-industry ladder; cache = pennies)" \
  python -u reclassify_levels.py

step "4/6 phase3 insights snapshot (--llm: titles + seniority + narrative)" \
  python -u phase3_insights.py --llm

step "5/6 re-embed (semantic search vectors)" \
  npm --prefix ../web run embed

step "6/6 sync pipeline DB -> web snapshot" \
  npm --prefix ../web run sync-db

# Optional 6th step — the batch scorecard (model-card report on this chunk:
# Coverage/Accuracy/Identity/Richness/Coherence/Corroboration/Cost + trend +
# cause->lever diagnosis, persisted to data/scorecard.jsonl). Free + deterministic
# by default. Opt in by setting SCORECARD=1 (add --llm yourself for the paid
# narrative). Kept off the default path so finalize stays zero-cost.
if [ "${SCORECARD:-0}" = "1" ]; then
  step "6/6 batch scorecard (since last run)" \
    python -u scorecard.py
fi

echo ""
echo "===== FINALIZE PASS — DONE $(date) ====="
echo "Remember to commit web/data/titans.db to ship the refreshed snapshot."
