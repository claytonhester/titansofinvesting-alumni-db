# Titans of Investing — Pipeline Runbook

Everything you need to collect, enrich, and serve the alumni dataset.

---

## Overview

The project has two halves:

| Half | What it does | Where it lives |
|---|---|---|
| **Pipeline** | Collects and enriches alumni data into a SQLite DB | `pipeline/` |
| **Web app** | Serves a chat interface and alumni directory | `web/` |

The pipeline runs once (or periodically). The web app reads the DB the pipeline produces.

---

## Prerequisites

### 1. Environment keys

Copy `.env.example` to `.env` at the repo root and fill in:

```bash
cp .env.example .env
```

| Key | Required | What it does | Where to get it |
|---|---|---|---|
| `FIRECRAWL_API_KEY` | **Yes** | Web scraping + search per person | [firecrawl.dev](https://firecrawl.dev) |
| `ANTHROPIC_API_KEY` | **Yes** | Claude extraction + identity resolution | [console.anthropic.com](https://console.anthropic.com) |
| `PDL_API_KEY` | Optional | Structured career data (charged ~$0.28/match) | [peopledatalabs.com](https://peopledatalabs.com) |
| `GNEWS_API_KEY` | Optional | News mentions (flat monthly subscription) | [gnews.io](https://gnews.io) |
| `PERPLEXITY_API_KEY` | Optional | Identity-verified public mentions/profiles (~$0.005/person + a small Haiku call) | [perplexity.ai/settings/api](https://www.perplexity.ai/settings/api) |

PDL and GNews have free tiers (100 records/month and 100 requests/day). If keys are absent the pipeline skips those sources gracefully — it still works, just with less data.

The web app also needs `ANTHROPIC_API_KEY` in `web/.env.local`:
```bash
echo "ANTHROPIC_API_KEY=your_key_here" >> web/.env.local
```

### 2. Python environment

```bash
cd pipeline
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Firecrawl credits

Check your balance before running:
```bash
python -c "from firecrawl import Firecrawl; from config import require_key; from cost_log import remaining_credits; print(remaining_credits(Firecrawl(api_key=require_key('FIRECRAWL_API_KEY'))), 'credits')"
```

**Credit budget:**
- ~5 credits/person for the career pass (main discovery, capped at 5 scrapes)
- ~5 credits/person for the news pass (press articles, capped at 5 scrapes)
- ~10 credits/person total maximum
- 100-person test run: ~1,000 credits
- Full ~1,200-person run: ~12,000 credits

---

## Running the pipeline

### Phase 1 — Ingest the alumni directory

Scrapes the Titans of Investing directory and populates the `people` table. Free — no API calls.

```bash
python cli.py ingest
```

Run once. Re-running is safe (idempotent).

**Result:** `pipeline/data/titans.db` populated with ~1,056 people.

---

### Start fresh (wipe collected data and re-ingest clean)

Two ways to reset, depending on how much you want to keep:

```bash
# A. Nuke EVERYTHING and rebuild from the directory (people + all enrichment).
rm pipeline/data/titans.db
python cli.py ingest                    # rebuilds the people table, clean by construction
python phase2_enrich.py --limit 50      # collect a fresh batch

# B. Keep the people list, wipe ONLY the enrichment (claims/status/sources).
sqlite3 pipeline/data/titans.db "DELETE FROM claims; DELETE FROM batch_status; DELETE FROM identity_candidates; DELETE FROM person_sources;"
python phase2_enrich.py --limit 50
```

Then `cd web && npm run sync-db` to push the new data to the site.

> You do **not** need `clean_data.py` or `renormalize_claims.py` for a fresh
> ingest — those are one-time/legacy backfill tools. Cleaning is built into the
> ingest itself (see next note).

### How data is cleaned (automatic — no manual step)

Every claim, from every source, passes through one gate (`normalize.digest_claims`)
right before it's written, which:
- **Title-cases** professional values ("kbre" → "KBRE", "texas a&m" → "Texas A&M").
- **Drops junk** values (a boolean/placeholder like "True" or "N/A" never gets stored).
- **De-duplicates** exact case-insensitive repeats from overlapping sources.

The web app then does a second cleaning pass at render time (`web/lib/`): it
re-title-cases everything, **merges duplicate jobs** (a dated role + its dateless
prose twin collapse into one; multiple roles at one employer stack under that
company), **groups education** (one card per school, degrees listed once), and
hides any junk that slipped through. So a person with zero prior data comes in,
gets cleaned on write, and is sorted + de-duplicated on display — start to finish,
no hand-fixing.

### Verified public mentions (Perplexity + Haiku)

When `PERPLEXITY_API_KEY` is set, Phase 2 also runs a discovery pass that beat
GNews/GDELT badly in testing (see `news_experiment.py`):

1. **Perplexity Search** for the person (name + employer) — finds bios, firm
   leadership pages, FINRA records, profiles, press.
2. **Drop aggregator domains** — people-search / data-broker junk (`news_score`).
3. **Claude Haiku identity check** (`news_verify`) — confirms each result is
   actually *this* person, not a namesake (kills the "Confederate general named
   Thomas Green" problem string-matching can't).

Survivors are stored as `public_links` claims → they render in the web
"Mentions & appearances" section, and like all name-search results stay OUT of
the hard résumé. Cost ≈ **$0.005/person + a small Haiku call** (~$7 for the full
base). Key-gated and never-raises: unset the key and the pass simply skips.

To experiment with strategies/sources before a big run:
```bash
python news_experiment.py --limit 12 --sources perplexity --verify --drop-aggregators
```

---

### Phase 2 — Enrich alumni profiles

Searches the web, scrapes sources, runs Claude extraction, and writes structured claims per person.

```bash
# Smoke test — 5 people
python phase2_enrich.py --limit 5

# Larger batch
python phase2_enrich.py --limit 100

# Drain everything remaining (set limit > total pending)
python phase2_enrich.py --limit 1200

# One specific person
python phase2_enrich.py --name "Jason Kaspar"
```

**Resumable:** Each person is committed individually. A crash or Ctrl+C resumes from where it left off — already-enriched people are skipped automatically.

**Output per person (printed to stdout):**
```
=== Jason Kaspar | Veritas Ark Fund | Austin ===
  Jason Kaspar: 4 sources -> 3 accepted (2 by pre-filter), 0 to review;
  22 claims (+synth bio) (+2 PDL) (+1 GNews) (+1 press); 1 sent to Sonnet; 9 credits total
```

**Cost per person (approximate):**
- Firecrawl: ~$0.008 (10 credits × $0.00083/credit)
- Claude Haiku: ~$0.033
- Claude Sonnet: ~$0.003
- PDL (if key set, if matched): ~$0.028–$0.28
- GNews: $0 (flat subscription, not per-call)
- **Total: ~$0.04–$0.32/person depending on PDL match**

Costs are logged to `pipeline/data/cost_log.jsonl` after each run.

---

### Phase 2 (backfill) — Add news + PDL to already-enriched people

If you enriched people before PDL/GNews/press-news keys were set, use this to layer those sources on top without re-running the expensive Firecrawl discovery:

```bash
# All already-enriched people
python enrich_news_only.py

# One specific person
python enrich_news_only.py --name "Jason Kaspar"
```

Safe to re-run — clears prior news and PDL rows before writing fresh ones.

---

### Phase 3 — Build insights and rollups

Calculates aggregate statistics across the enriched dataset. Run after Phase 2 is complete.

```bash
# Basic rollups only
python phase3_insights.py

# With LLM-generated narrative summaries (costs additional Claude tokens)
python phase3_insights.py --use-llm

# Specific class year only
python phase3_insights.py --year 2020
```

---

## Cost log

Every enrichment run appends a line to `pipeline/data/cost_log.jsonl`. Fields:

| Field | Meaning |
|---|---|
| `timestamp` | ISO 8601 UTC timestamp of the run |
| `label` | Run label (`enrich-5`, `"Jason Kaspar"`, etc.) |
| `people` | Number of people processed this run |
| `firecrawl_credits` | Credits consumed (measured from live meter delta, or estimated from scrape count if meter unavailable) |
| `firecrawl_credits_estimated` | `true` = fell back to scrape-count estimate; `false` = authoritative meter delta |
| `firecrawl_usd` | Firecrawl cost in USD |
| `haiku_tokens_in/out` | Claude Haiku input/output tokens (career + bio + news extraction) |
| `sonnet_tokens_in/out` | Claude Sonnet input/output tokens (identity resolution only) |
| `claude_usd` | Total Claude cost in USD |
| `total_usd` | `firecrawl_usd + claude_usd` (does NOT include PDL) |
| `pdl_matches` | Number of people PDL matched |
| `gnews_requests` | GNews requests made (informational only — GNews is flat subscription, not per-call) |

PDL cost is not in `total_usd` — it's tracked separately by match count. At ~$0.28/match, multiply `pdl_matches` by $0.28 to estimate PDL spend.

---

## Running the web app

```bash
cd web
npm install
npm run dev        # development, port 3210
npm run build      # production build check
npm start          # production, port 3210
```

**The web app reads its own bundled copy of the DB at `web/data/titans.db`, NOT the pipeline's.** This is so the site can deploy to Vercel (where `pipeline/` isn't in the build). `npm run dev` and `npm run build` run `sync-db` automatically (via `predev` / `prebuild`), copying the latest `pipeline/data/titans.db` → `web/data/titans.db`.

**After any enrichment run, refresh the site's data with:**
```bash
cd web && npm run sync-db      # copies pipeline DB -> web/data/titans.db
```
Then commit `web/data/titans.db` and redeploy if you're on Vercel. If the site looks stale after enriching, this is almost always the missing step.

---

## Troubleshooting

**"FIRECRAWL CREDITS EXHAUSTED"**
Top up at [firecrawl.dev](https://firecrawl.dev). Already-processed people are saved. Re-run the same command to continue.

**"Nothing to enrich (all targets done or none matched)"**
All people are already enriched. Use `--name` to re-enrich a specific person, or check batch_status:
```bash
sqlite3 pipeline/data/titans.db "SELECT phase, status, COUNT(*) FROM batch_status GROUP BY phase, status;"
```

**"RuntimeError: FIRECRAWL_API_KEY is not set"**
Add the key to `.env` at the repo root (not inside `pipeline/`).

**Check how many people are pending vs done:**
```bash
sqlite3 pipeline/data/titans.db "
  SELECT
    COUNT(*) FILTER (WHERE b.status = 'done')  AS done,
    COUNT(*) FILTER (WHERE b.status = 'error') AS errored,
    COUNT(*) FILTER (WHERE b.status IS NULL)   AS pending
  FROM people p
  LEFT JOIN batch_status b ON b.person_id = p.id AND b.phase = 'structuring'
  WHERE p.needs_review = 0;"
```

**Re-enrich a person who errored:**
```bash
# Reset their status
sqlite3 pipeline/data/titans.db "DELETE FROM batch_status WHERE person_id = (SELECT id FROM people WHERE full_name = 'Jane Doe');"
# Re-run
python phase2_enrich.py --name "Jane Doe"
```
