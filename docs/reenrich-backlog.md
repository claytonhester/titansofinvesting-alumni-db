# Re-enrichment backlog — Classes 1 & 2 (Texas A&M + UT)

Run date: **2026-06-10**. This documents people from the class 1 & 2 (A&M + UT) batch
who were **not enriched, or only thinly enriched**, so we know to revisit them. The
clean-run rule was honored: **no degraded/PDL-less profiles were written** — people we
couldn't fully enrich were left pending, not half-saved.

## 1. PENDING — never enriched (PDL monthly quota hit mid-run)

The PDL Person-Enrichment free quota (100/mo) ran out partway through UT class 2. The
pipeline stopped cleanly (HTTP 402 → rolled back the in-flight person, left the rest
pending). **Re-run these when PDL renews (~2026-07, ~27 days) or after a PDL top-up:**

| Person | School | Class |
|---|---|---|
| Helen Xiang | University of Texas | 2 |
| Azure Yu | University of Texas | 2 |

Re-run: `python phase2_enrich.py --class 2 --school "University of Texas" --limit 25 --max-credits 100`
(skips everyone already done).

## 2. THIN — enriched this run but sparse (no PDL match + empty Jina baseline)

These completed a full pass but have few résumé claims, because they had **no PDL
match** AND the free **Jina baseline discovery returned 0 sources** (see root cause
below). They're marked done, so a re-run won't pick them up automatically — revisit
explicitly (e.g. by `--name`) after the Jina-baseline fix and/or a PDL top-up.

| Person | School | Class | Claims | Note |
|---|---|---|---|---|
| Sofia Kampfe | UT | 1 | 0 | no footprint found |
| Mickey Li | UT | 1 | 1 | has a fintech-panel news item |
| David Phillips | A&M | 1 | 2 | |
| Bart Howe | A&M | 2 | 2 | **has 2 good news items** (AHIOS President; Entrepreneur of the Year) — résumé thin, press fine |
| Payal Patel | UT | 1 | 3 | **has 2 leadership-move news items** — résumé thin, press fine |

## 3. THIN — pre-existing (from the 2026-06-09 run, not this batch)

Already-done UT class 2 people that are also thin; worth a re-look on the next pass:

| Person | Claims | Note |
|---|---|---|
| Ricardo Lopez | 0 | known namesake/identity-broker issue (see identity hardening notes) |
| Alan Boyd | 0 | |
| James Miller | 0 | common name — likely identity-gated out |
| Priyanka Suri | 0 | |

## Root cause to fix (affects only no-PDL-match people)

The free **Jina baseline discovery** (`jina_discovery.discover_via_jina`) returned **0
usable sources for essentially everyone** this run (Perplexity `/search` → Jina fetch).
PDL-matched people were unaffected (PDL + the deep Firecrawl path produced rich 28–40
claim profiles), but **no-PDL-match people had nothing to build from** → the thin
profiles above. Likely cause: Jina free-tier rate-limiting/blocking under sustained
batch load (global 3.2s throttle, ~20 req/min), or `/search` returning only
aggregator/social URLs that get dropped. **Diagnose before the next large batch** — if
the Firecrawl-free baseline isn't producing sources, low-signal people can't be
enriched without leaning on paid Firecrawl.

## Itemized cost of this run

**Authoritative cash spend ≈ $2.50–3.50.** The `cost_log.jsonl` "total" of **$11.62**
is *notional* — it prices PDL at $0.28/match, but we are on PDL's **free tier**, so the
29 matches cost **$0** cash.

| Provider | Usage | Cash |
|---|---|---|
| PDL (person) | 29 matches / ~36 calls | **$0** (free tier; quota now spent) |
| Perplexity — Sonar press | 190 calls | **$1.47** |
| Perplexity — /search | 37 calls | **$0.19** |
| Firecrawl (deep path) | 565 credits (meter 1431→866) | **~$0.47** |
| Anthropic (Haiku/Sonnet) | structuring/identity/curate/verify | **~$0.5–1** |
| **Total cash** | | **~$2.5–3.5** |
| cost_log "measured total" (notional, incl. free PDL) | 37 people | $11.62 |

Per-cohort cost_log entries (4 rows, labels `Texas A&M-1/2`, `University of Texas-1/2`)
are appended to `pipeline/data/cost_log.jsonl`.

## Credits remaining after this run
- **Firecrawl: 866 credits.**
- **PDL Person: 0 (exhausted, renews ~27 days).** PDL Company: ~60.
- **Perplexity: funded** (held through the run). **Anthropic: fine.**

## Result summary
- Enriched this run: **37 people** (A&M c1 15/15, A&M c2 5/5, UT c1 16/16, UT c2 5/7).
- Cohorts now complete: **51 / 53** (only the 2 pending above remain).
- **15 curated news items** surfaced, correctly categorized across law/healthcare/RE/PE/
  fintech (de-bias confirmed). 0 errors.
