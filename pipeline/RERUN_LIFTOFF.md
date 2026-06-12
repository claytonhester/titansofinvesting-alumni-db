# Liftoff runbook — complete rerun of already-enriched alumni

Rebuild every already-enriched person (~110) on the **current** pipeline, because
their data was built by volatile dev-era versions (pre-PDL, pre-search-URL,
pre-reconciler, pre-identity-hardening). Then deep-pass the thin survivors.

All commands run from `pipeline/` with the venv active: `source .venv/bin/activate`.
**Nothing here has been run yet.** Wait until you're ready, then go top to bottom.

---

## 0. Pre-flight (read-only — spends nothing)

```bash
python preflight.py
```
Must print **`=== GO ===`**. It checks: required keys present, a DB backup exists,
Firecrawl balance, target count (~110) and the ~$44 cost estimate. If NO-GO, fix
the `✗` line (usually: make a backup) and re-run.

Also confirm the suite is green:
```bash
python -m pytest tests -q          # expect: 750 passed
```

## 1. Back up the DB (if preflight flagged it)

```bash
cp data/titans.db "data/titans.backup.$(date +%F)-prererun.db"
```
The web app reads the *synced* `web/data/titans.db`, so nothing below goes live
until step 6. Pipeline runs only touch `pipeline/data/titans.db`.

## 2. Base-sweep rebuild of all enriched people (PDL + URL + news, $0 Firecrawl)

```bash
python phase2_enrich.py --rerun-enriched --limit 200 --max-credits 0
```
- `--max-credits 0` → **zero Firecrawl** (no flaky reads here; deferred to step 4).
- `--limit 200` covers all ~110. Each person commits before the next (resumable-ish).
- **Hard stops (by design):** PDL quota spent → exit 3, stops cleanly, current
  person rolled back. 3 errors in a row (systemic) → exit 4, stops. A one-off bad
  profile → marked `error`, rolled back, run continues. **No half-built profile is
  ever saved.**
- Watch the exit code: `0` = all done, `3` = PDL quota (top up, re-run), `4` =
  systemic (fix the cause, re-run). On a re-run it rebuilds from the top
  (idempotent; re-spends on the already-done — fine for ~110).

> PDL note: ~110 matches ≈ $31 of PDL. Make sure the monthly quota covers it, or
> it'll hard-stop partway (which is safe, just incomplete).

## 3. Score + flag the rebuilt profiles (free)

```bash
python compute_completeness.py
```
Prints the deep-search queue count. Sets `needs_deep_search` on the thin ones,
clears it on the now-rich ones.

## 4. Deep pass — Firecrawl LinkedIn read on the thin survivors only

```bash
python phase2_enrich.py --needs-deep --limit 200 --max-credits 250
```
- Targets only `needs_deep_search=1`, forces REFRESH, reads the search-corrected
  LinkedIn URL. Lands ~42% (the rest are genuine ghosts — the read refuses for ~0
  credits). Sets `deep_search_done` so each person is tried **at most once** (the
  queue drains; no re-spend on re-run).
- `--max-credits 250` caps Firecrawl per person. Firecrawl balance is ~87k credits.

## 5. Re-score + the "couldn't enrich" report (free)

```bash
python compute_completeness.py        # drains the deep-done people from the queue
python preflight.py --report
```
The report lists, separately: **errored** (safe to re-run), **zero-claim**
(genuine ghosts — no footprint anywhere), and **thin/flagged** (enriched but
still incomplete). This is the "note who we couldn't enrich" list.

## 6. Publish (only when the data looks right)

```bash
SCORECARD=1 ./finalize_pass.sh        # sectors, completeness, insights, embed, sync-db, scorecard
```
The scorecard hard-gate must PASS (no future-date P0, no gold violation). This is
also what copies the pipeline DB → `web/data/titans.db` (the live snapshot).

---

## If something goes wrong
- **Stops with exit 3/4:** expected fail-safe. Read the message, fix (top up PDL /
  fix the API), re-run the same command. Completed people are saved.
- **Want to undo:** restore the backup — `cp data/titans.backup.<date>-prererun.db data/titans.db`.
- **A specific person looks wrong:** `python phase2_enrich.py --ids <id> --max-credits 0`
  then `--needs-deep` rebuilds just them.
