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
#   6. npm run sync-db         — copy pipeline DB -> web/data/titans.db (the LOCAL
#                                dev snapshot; gitignored, never committed).
#   7. make_display_db.py      — build data/titans_display.db: the hosted copy with
#                                the internal tables emptied. The operator then
#                                uploads it to the private Vercel Blob and redeploys.
#
# Idempotent and safe to re-run. Steps 1-4 are independent of each other, so a
# failure there is reported and the rest still run — BUT the pass aborts before
# steps 5-7: embedding and shipping a snapshot whose scores/titles/levels are stale
# would silently publish half-finalized data.
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

NFAIL=0
FAILED=""
step() {
  local label="$1"
  shift
  echo ""
  echo "----- $label -----"
  if "$@"; then
    echo "  ok"
  else
    local rc=$?
    echo "  !! step failed (rc=$rc)"
    NFAIL=$((NFAIL + 1))
    FAILED="${FAILED}  - ${label} (rc=${rc})"$'\n'
  fi
}

# Hard stop: refuse to continue to the ship-side steps if anything above failed.
abort_if_failed() {
  if [ "$NFAIL" -gt 0 ]; then
    echo ""
    echo "===== FINALIZE PASS — ABORTED before '$1' ($NFAIL step(s) failed) ====="
    printf '%s' "$FAILED"
    echo "Fix the cause and re-run ./finalize_pass.sh (every step is idempotent)."
    exit 1
  fi
}

echo "===== FINALIZE PASS — START $(date) ====="

step "1/7 reclassify sectors (Haiku catch-all upgrade)" \
  python -u reclassify_sectors.py

step "2/7 profile completeness scores (free, deterministic)" \
  python -u compute_completeness.py

step "3/7 reclassify seniority levels (cross-industry ladder; cache = pennies)" \
  python -u reclassify_levels.py

step "4/7 phase3 insights snapshot (--llm: titles + seniority + narrative)" \
  python -u phase3_insights.py --llm

abort_if_failed "5/7 re-embed"

step "5/7 re-embed (semantic search vectors)" \
  npm --prefix ../web run embed

abort_if_failed "6/7 sync-db"

step "6/7 sync pipeline DB -> web/data/titans.db (local dev snapshot)" \
  npm --prefix ../web run sync-db

abort_if_failed "7/7 display DB"

step "7/7 build display-only DB for hosting (data/titans_display.db)" \
  python -u make_display_db.py

abort_if_failed "scorecard / finish"

# Optional step — the batch scorecard (model-card report on this chunk:
# Coverage/Accuracy/Identity/Richness/Coherence/Corroboration/Cost + trend +
# cause->lever diagnosis, persisted to data/scorecard.jsonl). Free + deterministic
# by default. Opt in by setting SCORECARD=1 (add --llm yourself for the paid
# narrative). Kept off the default path so finalize stays zero-cost.
if [ "${SCORECARD:-0}" = "1" ]; then
  step "batch scorecard (since last run)" \
    python -u scorecard.py
fi

echo ""
echo "===== FINALIZE PASS — DONE $(date) ====="
echo "To ship: upload pipeline/data/titans_display.db to the private Vercel Blob"
echo "(the file TITANS_DB_URL points at) and redeploy. Do NOT commit any titans*.db —"
echo "web/data/sample.db (synthetic) is the only database tracked in git."
