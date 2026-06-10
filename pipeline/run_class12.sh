#!/usr/bin/env bash
# Enrich classes 1 & 2 at Texas A&M + UT. Stops the WHOLE sequence cleanly the moment
# PDL quota is exhausted (phase2_enrich exits 3) — remaining people are left pending,
# never enriched on a degraded PDL-less path.
set -u
cd "$(dirname "$0")"
source .venv/bin/activate

fc() { python -c "import config,os;from firecrawl import Firecrawl;from cost_log import remaining_credits;print(remaining_credits(Firecrawl(api_key=os.environ['FIRECRAWL_API_KEY'])))" 2>/dev/null; }

echo "===== CLASS 1&2 @ A&M + UT — START $(date) ====="
echo "Firecrawl before: $(fc)"

run_cohort() {
  local cls="$1" school="$2" cap="$3"
  echo "----- class $cls @ $school (cap $cap) -----"
  python -u phase2_enrich.py --class "$cls" --school "$school" --limit 25 --max-credits "$cap"
  local rc=$?
  if [ "$rc" -eq 3 ]; then
    echo "===== PDL QUOTA EXHAUSTED — leaving remaining cohorts un-enriched ====="
    return 3
  fi
  return 0
}

run_cohort 1 "Texas A&M" 350 \
  && run_cohort 2 "Texas A&M" 150 \
  && run_cohort 1 "University of Texas" 400 \
  && run_cohort 2 "University of Texas" 200

echo "Firecrawl after: $(fc)"

# Always finalize — even if PDL quota stopped the run early, the people who DID
# enrich still need the sector Haiku upgrade, the --llm snapshot, embeddings, and
# the web sync. finalize_pass.sh is idempotent.
echo "----- finalizing (reclassify -> phase3 --llm -> embed -> sync-db) -----"
./finalize_pass.sh

echo "===== DONE $(date) ====="
