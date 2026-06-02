# Phase 2 Enrichment — Cost Analysis

**What this costs to build the intelligence DB for the Titans alumni.**
Two vendors bill us: **Firecrawl** (search + scrape) and **Anthropic** (Claude).
Numbers below are list-price estimates; the pipeline records the *authoritative*
figure to `data/cost_log.jsonl` on every real run (Firecrawl from the live
credit-meter delta, Claude from actual token counts).

Directory size today: **1,056 people** (`SELECT COUNT(*) FROM people`).

---

## Per-person cost (the unit that scales)

### 1. Firecrawl — search then scrape only the keepers
We **search first** (cheap metadata, not credit-counted here), dedupe by domain,
rank, and **scrape only the top survivors** — at most `max_sources = 8` pages,
**1 credit each**. Plan price: 100,000 credits for $83 → **$0.00083 / credit**.

| | scrapes/person | Firecrawl $/person |
|---|---|---|
| Worst case (hit the cap) | 8 | $0.0066 |
| Typical (5–6 distinct good domains) | ~5 | $0.0042 |

> This is the change that mattered: the old path scraped ~20 pages/person
> *before* dedup (~$0.017/person) and threw most away. Search-first cuts the
> scrape count — and the bill — to the keepers.

### 2. Anthropic — two models, two jobs
- **Haiku** (`claude-haiku-4-5`) — structuring. Reads up to 8 sources × 12k chars
  (~26k input tokens), emits the profile JSON (~1.5k output). Price $1 / $5 per Mtok.
- **Sonnet** (`claude-sonnet-4-6`) — the identity merge gate, one call/person.
  Reads 8 × 2k-char snippets (~4.5k input), emits verdicts (~0.6k output). Price $3 / $15 per Mtok.

| Model | $/person |
|---|---|
| Haiku structuring | ~$0.034 |
| Sonnet identity gate | ~$0.023 |
| **Claude subtotal** | **~$0.057** |

> **Correction vs. the earlier ~$50 estimate:** that figure counted Haiku only and
> silently dropped the Sonnet identity call. Sonnet runs once per person and is the
> more expensive tier, so the real per-person Claude cost is ~$0.057, not ~$0.034.
> `cost_log.py` now prices both models.

### Combined per person
| | $/person |
|---|---|
| Conservative (cap on everything) | **~$0.063** |
| Likely (typical source counts / shorter pages) | **~$0.045** |

---

## One-time run totals

| Run | People | Likely (~$0.045) | Conservative (~$0.063) |
|---|---|---|---|
| **Full** | 1,056 | **~$48** | **~$67** |
| **First half** | 528 | **~$24** | **~$33** |

Budget headline: **plan ~$50 for the full one-time build, ~$25 for the first half.**
Firecrawl is a rounding error here (~$7 full / ~$3.50 half); **Claude is ~90% of
the cost**, and Sonnet is the largest single line item.

---

## What could move these numbers
- **Fewer sources/person** (lower `max_sources`) → linear savings on Firecrawl
  *and* Haiku input tokens. The cheapest lever.
- **Shorter source caps** (`_MAX_SOURCE_CHARS`, identity snippet 2k) → less Haiku/Sonnet input.
- **Opus escalation** for ambiguous identities (planned, not yet wired) would add
  cost only on the fraction of people sent to review — small in aggregate.
- **Re-runs** (refreshing existing profiles) cost the same per person as a first
  run; they are not cheaper. See `RERUN_PLAN.md`.

> **Authoritative source of truth:** after any real batch, read
> `data/cost_log.jsonl`. The headline there is the measured Firecrawl credit delta
> + real Claude tokens for that run — trust it over this estimate.
