# Liftoff runbook — targeted rerun of the struggling profiles

Rebuild only the already-enriched profiles a quality triage flags as struggling
(built on volatile dev-era versions: pre-PDL, pre-search-URL, pre-reconciler).
The triage (2026-06-11) found only **34 of 110 need it** — 26 are SOLID and 50
are GOOD, so we leave 76 alone. The 34 fit the current budget (~$14) with no
top-up. Then deep-pass the thin survivors.

All commands run from `pipeline/` with the venv active: `source .venv/bin/activate`.

> **Status: EXECUTED (June 11–12, 2026).** The triage rerun (84 people across the
> base sweep + deep passes) ran to completion: cohort scorecard **D/66 → B/84**,
> deep-search queue drained. Kept as the reference procedure; the generic version
> lives in `RUNBOOK.md` → "Rerun / triage flow". Edits on 2026-09-03: test count,
> `--max-credits` semantics, and the publish step (no DB is committed any more).

---

## 0. Triage + pre-flight (read-only — spends nothing)

```bash
python profile_triage.py          # see the SOLID/GOOD/WEAK/BROKEN breakdown
python preflight.py               # must print === GO ===
python -m pytest tests -q         # expect: ≈790 passed
```
`profile_triage.py` grades every profile on completeness, coherence,
corroboration, and PDL spine. The **rerun set = WEAK+BROKEN**. Eyeball it; if a
SOLID/GOOD person actually looks wrong, you can add them by id in step 2.

## 1. Back up the DB

```bash
cp data/titans.db "data/titans.backup.$(date +%F)-prererun.db"
```
The web app reads the *synced* `web/data/titans.db` locally and the Blob-hosted
display DB in production, so nothing below goes live until step 6. Pipeline runs
only touch `pipeline/data/titans.db`.

## 2. Rebuild the struggling profiles (PDL + URL + news, $0 Firecrawl)

```bash
python phase2_enrich.py --ids "$(python profile_triage.py --rerun-ids)" --max-credits 0
```
- Reruns exactly the WEAK+BROKEN set (~34). Add `--include-good` to the inner
  command to also rebuild GOOD; or hand-edit the id list.
- `--max-credits 0` → **zero Firecrawl** (no flaky reads here; deferred to step 4).
- Budget: ~$14 (well under PDL 86cr / Claude $3.89 / Perplexity $5.36).
- **Hard stops (by design):** PDL quota spent → exit 3, stops cleanly, current
  person rolled back. 3 errors in a row (systemic) → exit 4, stops. A one-off bad
  profile → marked `error`, rolled back, run continues. **No half-built profile is
  ever saved.**
- Watch the exit code: `0` = all done, `3` = PDL quota (top up, re-run), `4` =
  systemic (fix the cause, re-run). `--ids` re-runs exactly the list each time,
  so a re-run redoes the whole set (idempotent; re-spends — fine for ~34).

> PDL note: ~34 matches, and 20 of them are BROKEN (mostly ghosts that miss PDL
> = no credit), so realistic PDL use is well under the 86-credit balance.

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
- `--max-credits 250` is a **run-level** ceiling on deep-path Firecrawl credits
  (not per person) — size it at roughly `targets × 200` for a deep pass. Firecrawl
  balance was ~87k credits at the time.

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
The scorecard hard-gate must PASS (no future-date P0, no gold violation). `sync-db`
refreshes the local (gitignored) `web/data/titans.db`; to update production, run
`python make_display_db.py` and upload `data/titans_display.db` to the private Vercel
Blob (`vercel blob put … --force`), then redeploy — see `RUNBOOK.md` → "Shipping data".

---

## If something goes wrong
- **Stops with exit 3/4:** expected fail-safe. Read the message, fix (top up PDL /
  fix the API), re-run the same command. Completed people are saved.
- **Want to undo:** restore the backup — `cp data/titans.backup.<date>-prererun.db data/titans.db`.
- **A specific person looks wrong:** `python phase2_enrich.py --ids <id> --max-credits 0`
  then `--needs-deep` rebuilds just them.
