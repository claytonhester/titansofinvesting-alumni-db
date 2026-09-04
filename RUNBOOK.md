# Titans of Investing — Pipeline Runbook

Operator's guide: collect, enrich, finalize, and ship the alumni dataset.
Every command runs from `pipeline/` with the venv active unless noted.
*Last verified against the code: 2026-09-03.*

---

## Overview

| Half | What it does | Where |
|---|---|---|
| **Pipeline** | Collects + enriches alumni into `pipeline/data/titans.db` | `pipeline/` |
| **Web app** | Serves directory, insights, chat from a read-only SQLite snapshot | `web/` |

The public repo ships only the synthetic `web/data/sample.db`. The real DB never
enters git; production downloads a display-only copy from a private Vercel Blob at
build time (see [Shipping data](#shipping-data-to-the-site)).

---

## Prerequisites

### 1. Keys — `.env` at the repo root

```bash
cp .env.example .env     # fill in; config.py loads REPO_ROOT/.env
```

| Key | Required? | Role in a run |
|---|---|---|
| `ANTHROPIC_API_KEY` | **Yes** | Haiku structuring/verification/reconcile; Sonnet identity gate |
| `FIRECRAWL_API_KEY` | **Yes — key only** | Needed to *construct* the client. **Zero credits is fine**: the baseline path is Firecrawl-free. Credits are spent only on the deep path (see below) |
| `PDL_API_KEY` | Soft | People Data Labs match — the résumé "spine" (~$0.28/match, misses free). Unset → PDL step skipped |
| `PERPLEXITY_API_KEY` | Soft | `/search` mention discovery + Sonar press discovery (both Haiku-verified). Unset → those passes skip |
| `GNEWS_API_KEY` | Legacy | Read only by the frozen `experiments/news_experiment.py`. Not used by enrichment |

`preflight.py` prints exactly this table with ✓/✗ for the current shell.

### 2. Python + Node

```bash
cd pipeline && python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
cd ../web && npm install                     # finalize_pass.sh calls `npm run embed` / `sync-db`
```

### 3. Firecrawl credits — what they actually buy

```bash
python -c "from firecrawl import Firecrawl; from config import require_key; from cost_log import remaining_credits; print(remaining_credits(Firecrawl(api_key=require_key('FIRECRAWL_API_KEY'))), 'credits')"
```

- **Baseline (free):** PDL + Perplexity `/search` + Jina fetch + Sonar press. 0 credits.
- **Deep path (billed):** richer career scrape + Firecrawl news + the LinkedIn agent
  read (**~45–324 credits per read**, ~126–200 typical). It fires only when the
  signal gate says so (`deep_gate.is_high_signal`: PDL match, or ≥2 verified sources
  + a current employer) or a policy forces it, and always under `--max-credits`.
- `--max-credits N` is a **run-level ceiling** on deep-path credits (default = live
  balance). `--max-credits 0` = a guaranteed Firecrawl-free run.

---

## Phase 1 — Ingest the directory (free)

```bash
python cli.py ingest                    # scrape the public class directory -> people
python cli.py ingest --html snap.html   # re-parse a saved snapshot
```
Idempotent. Result: `people` table (1,056 rows as of June 2026).

---

## Phase 2 — Enrich (`phase2_enrich.py`)

```bash
python phase2_enrich.py --limit 5                         # next 5 un-enriched
python phase2_enrich.py --name "Jonah Kessler"            # one person
python phase2_enrich.py --class 2 --school "Texas A&M" --limit 25
python phase2_enrich.py --ids 770,817 --max-credits 0     # rebuild these ids, no Firecrawl
```

### Flags

| Flag | Meaning |
|---|---|
| `--limit N` | How many un-enriched people (default 5) |
| `--name "Full Name"` | One person by name |
| `--class N` / `--school S` | Target an un-enriched cohort |
| `--ids a,b,c` | Rebuild exactly these ids **in place** (bypasses the done-check) |
| `--rerun-enriched` | Rebuild everyone with a `person_insights` row (pair with `--max-credits 0`) |
| `--needs-deep` | Deep pass: only `person_insights.needs_deep_search=1`; forces `--policy refresh` |
| `--policy bulk\|deep\|refresh` | Gate criteria (`research_policy.py`): **bulk** = all gates; **deep** = deep Firecrawl path fires for everyone; **refresh** = deep + LinkedIn agent fires even on complete-looking profiles |
| `--max-credits N` | Run-level ceiling on deep Firecrawl credits (not per person) |
| `--max-usd X` | Run-level USD ceiling across all billed vendors |
| `--no-pdl` | Hard-disable PDL for this run (save the monthly quota) |
| `--force-deep` | Deprecated alias for `--policy deep` |

Policies change *which gates apply*; the spend ceilings stay active under every policy.

### Exit codes and stops

| Exit | Meaning | What to do |
|---|---|---|
| `0` | Finished the target set | — |
| `1` | Nothing to enrich / bad args / missing required key | Check targets or `.env` |
| `3` | **PDL quota exhausted** — current person rolled back, rest left pending | Top up / wait for renewal, re-run same command |
| `4` | **Systemic abort** — 3 errors in a row (API down, auth) | Fix the cause, re-run |
| `5` | **`--max-usd` ceiling hit** — stopped cleanly | Raise the cap or re-run later |
| — | `AuthError` (401/403 from a provider) aborts immediately | Fix the key |

Every person is committed individually; a crash or Ctrl+C resumes. A one-off error
marks that person `error` (rolled back) and continues. **No half-built profile is
ever saved.** Firecrawl credits exhausted mid-run aborts the batch (top up, re-run).

### Reading the log

```
=== Jonah Kessler | Veritas Ark Fund | Austin ===
  Deep Firecrawl: skipped (low signal — free baseline only)      # or: skipped (Firecrawl deep-path budget spent)
  Firecrawl LinkedIn: skipped (<reason>) | not found (N credits) | no credits — skipped
  Jonah Kessler: 4 sources -> 3 accepted ...; 22 claims (+2 PDL) (+2 verified mentions)
Run cost (measured): $0.39 for 1 people -> data/cost_log.jsonl
```

### What happens per person (short)

1. **Baseline:** PDL match (identity-anchored on the roster; extras pass a Haiku
   gate, `pdl_verify`) → Perplexity `/search` + Jina fetch → Sonnet identity gate →
   Haiku structuring → verified mentions (`news_verify`, strictly *about the person*).
2. **Deep (gated):** Firecrawl career scrape + news + LinkedIn read (search-corrected
   URL, `linkedin_verify` fail-closed).
3. **Write:** `reconcile.py` (Haiku; never invents, never drops, never raises) →
   `normalize.digest_claims` → claims, `person_insights`, `person_company`,
   `news_curated`, cost-log entry.

---

## Two-pass workflow (the standard way to run a cohort)

```bash
python phase2_enrich.py --limit 50 --max-credits 0     # 1. base sweep: Firecrawl-free
python compute_completeness.py                          # 2. score 0-100; sets needs_deep_search
python phase2_enrich.py --needs-deep --limit 200 --max-credits 2500   # 3. deep pass
python compute_completeness.py                          # 4. re-score; queue drains
```

- The flag rule (`deep_search_flag.py`): **no current role OR fewer than 3 career
  roles**. Bio/press/education gaps are deliberately *not* flagged.
- Step 3 sets the sticky marker `person_insights.deep_search_done=1`, so each person
  is deep-searched **at most once**; `compute_completeness` clears the flag on
  profiles that became rich. Expect ~42% of reads to land — the rest are ghosts.
- Size `--max-credits` for the deep pass at roughly `targets × 200`.

---

## Rerun / triage flow (already-enriched people)

```bash
python profile_triage.py                  # SOLID / GOOD / WEAK / BROKEN breakdown (free)
python preflight.py                       # keys, backup, targets, Firecrawl balance -> GO / NO-GO
cp data/titans.db "data/titans.backup.$(date +%F)-prererun.db"     # preflight requires one
python phase2_enrich.py --ids "$(python profile_triage.py --rerun-ids)" --max-credits 0
python compute_completeness.py
python phase2_enrich.py --needs-deep --limit 200 --max-credits 2500
python compute_completeness.py && python preflight.py --report     # errored / zero-claim / thin
```

`--rerun-ids` prints WEAK+BROKEN ids (`--include-good` adds GOOD).

**Backup-compare gate (do this by hand before finalizing).** `--ids` / `--rerun-enriched`
**wipe and rebuild** each target. A `--max-credits 0` rebuild can only re-fetch what
PDL/Perplexity return *today*; a profile whose richness came from a source that
can't be re-fetched (an old Firecrawl read, a page since taken down, a spent LinkedIn
read) can come back thinner. Compare claim counts against the backup before you ship:

```bash
sqlite3 data/titans.db "ATTACH 'data/titans.backup.<date>-prererun.db' AS b;
  SELECT n.person_id, (SELECT COUNT(*) FROM b.claims WHERE person_id=n.person_id) AS before, COUNT(*) AS after
  FROM claims n GROUP BY n.person_id HAVING after < before*0.7;"
```
Anyone who regressed: restore just them from the backup, or re-run with `--needs-deep`.

---

## Phase 3 + finalize (after ANY enrichment batch)

```bash
./finalize_pass.sh              # the 6 steps below
SCORECARD=1 ./finalize_pass.sh  # + batch scorecard (data/scorecard.jsonl; hard gate must PASS)
```

| # | Step | Cost |
|---|---|---|
| 1 | `reclassify_sectors.py` — reflow `current_sector`/`first_sector`; Haiku upgrades the catch-all | pennies |
| 2 | `compute_completeness.py` — 0-100 score + `needs_deep_search` flag | free |
| 3 | `reclassify_levels.py` — cross-industry seniority ladder (`seniority_v2`), cached Haiku, trajectory table | pennies |
| 4 | `phase3_insights.py --llm` — cohort snapshot (`--year` = snapshot year, default current UTC year). Must be `--llm` or titles/narrative revert to templated | 2 Haiku calls |
| 5 | `npm run embed` — rebuild `person_vectors` for semantic search | free (local model) |
| 6 | `npm run sync-db` — snapshot → `web/data/titans.db` (gitignored) | free |

Steps are independent; a failure is reported and the rest continue. The script's
closing reminder to "commit web/data/titans.db" is **stale** — see next section.

Standalone: `python phase3_insights.py` (free, templated), `--llm`, `--year 2026`.
`reclassify_*.py` accept `--dry-run` / `--no-llm`.

---

## Shipping data to the site

`web/scripts/sync-db.mjs` (runs on `predev`/`prebuild`) picks the DB in this order,
converting each to rollback-journal mode (WAL can't open read-only on Vercel):

1. `TITANS_DB_URL` set → download the real display DB (production)
2. `../pipeline/data/titans.db` → copy (local dev with real data)
3. existing `web/data/titans.db` → keep
4. `web/data/sample.db` → synthetic fallback (fresh clone)

**Refresh production** (nothing is committed — only `sample.db` lives in git):

```bash
./finalize_pass.sh
python make_display_db.py                # data/titans.db -> data/titans_display.db
                                         # empties identity_candidates, person_sources, batch_status, geocode_cache
# upload to the private Vercel Blob (operator, from the repo root with the Blob token in env):
vercel blob put pipeline/data/titans_display.db --force
# then redeploy (git push or `vercel --prod`); the build downloads TITANS_DB_URL
```

**Regenerate the public sample** after a schema change (never commit the real DB):
`python make_sample_db.py data/titans.db ../web/data/sample.db`.

Web env vars (`TITANS_DB_URL`, `ANTHROPIC_API_KEY`, `CHAT_TOKEN_SECRET`, `UPSTASH_*`)
are documented in `web/.env.example`.

---

## Cost log — `data/cost_log.jsonl`

One JSONL row per run (`cost_log.build_entry` / `append_entry`, append-only):

| Field | Meaning |
|---|---|
| `timestamp`, `label`, `people` | UTC time; run label (`enrich-5`, `deep-12`, `rerun-34`, `ids-3`, `Texas A&M-2`, a name); people processed |
| `firecrawl_credits`, `firecrawl_credits_estimated`, `firecrawl_usd` | Live meter delta (authoritative) or scrape-count estimate; $0.00083/credit |
| `haiku_tokens_in/out`, `sonnet_tokens_in/out`, `claude_usd` | Claude tokens and cost, both models |
| `pdl_matches`, `pdl_usd` | Matches × $0.28 (**notional** if you are on PDL's free tier) |
| `perplexity_requests`, `perplexity_usd` | `/search` calls × $0.005 |
| `sonar_requests`, `sonar_usd` | Sonar press calls; USD as reported by the API |
| `gnews_requests` | Always 0 in current runs (GNews retired; informational) |
| `total_usd` | `firecrawl + claude + pdl + perplexity + sonar` — **PDL is included** |

Rough all-in base-sweep cost: ~$0.40/person (`preflight.py` uses this).

---

## Start fresh

```bash
# A. Everything: rebuild from the directory
rm data/titans.db && python cli.py ingest

# B. Keep people, wipe all enrichment + derived tables
sqlite3 data/titans.db "DELETE FROM claims; DELETE FROM batch_status; DELETE FROM identity_candidates;
  DELETE FROM person_sources; DELETE FROM person_insights; DELETE FROM person_company;
  DELETE FROM news_curated; DELETE FROM person_role_levels; DELETE FROM insights_snapshot;
  DELETE FROM person_vectors;"
```
Leave `companies`, `role_level_cache`, `geocode_cache` — they are caches, not per-person
data, and keep re-runs cheap. (`person_vectors` is created by `npm run embed`; ignore
the error if it doesn't exist yet.) `clean_data.py` / `renormalize_claims.py` /
`reconcile_existing.py` are one-time backfill tools, not part of a fresh run.

---

## Troubleshooting

- **"Nothing to enrich (all targets done or none matched)"** — use `--name`, `--ids`,
  or `--rerun-enriched`. Status: `sqlite3 data/titans.db "SELECT phase,status,COUNT(*) FROM batch_status GROUP BY 1,2;"`
- **"RuntimeError: FIRECRAWL_API_KEY is not set"** — the key must exist even with 0
  credits. Put it in the repo-root `.env`, not `pipeline/`.
- **"Deep Firecrawl: skipped (Firecrawl deep-path budget spent)"** — `--max-credits`
  reached, or the balance is 0. Expected on a base sweep; top up for a deep pass.
- **Exit 3 / PDL 402** — the PDL dashboard count is not the API quota. Wait for the
  monthly reset or upgrade; pending people are untouched.
- **Overview looks stale after enriching** — you skipped `finalize_pass.sh`
  (phase2 writes per-person data only) or didn't re-upload the display DB.
- **Pending vs done:**
  ```bash
  sqlite3 data/titans.db "SELECT COUNT(*) FILTER (WHERE b.status='done') done,
    COUNT(*) FILTER (WHERE b.status='error') errored, COUNT(*) FILTER (WHERE b.status IS NULL) pending
    FROM people p LEFT JOIN batch_status b ON b.person_id=p.id AND b.phase='structuring' WHERE p.needs_review=0;"
  ```
- **Re-enrich someone who errored:** `python phase2_enrich.py --ids <id> --max-credits 0`.
- **Undo a run:** `cp data/titans.backup.<date>-<label>.db data/titans.db`.

Tests: `python -m pytest -q` (≈790 today) — CI runs them on every push.
