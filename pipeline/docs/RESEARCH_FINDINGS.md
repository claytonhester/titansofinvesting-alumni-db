# Research Findings: How Others Build People-Enrichment Pipelines

> Cross-domain review of established practice in people-enrichment / OSINT / record-linkage,
> mapped against the Titans pipeline. Goal: find where our design diverges from mature
> practice *before* scaling to 1,056 people and spending real money.

---

## 1. Where the pipeline already matches mature practice

- **Search-then-scrape-only-keepers.** `discovery.py` searches metadata-only across 5 angle
  queries, dedupes by domain, then scrapes only the top ~8 keepers (1 credit each). This is the
  correct cost discipline — no redundant scraping. Verified in `discovery.py`.
- **Confidence-banded human review.** AUTO_ACCEPT ≥ 0.85, REVIEW 0.40–0.85, reject < 0.40.
  This matches the documented consensus that **LLM-as-judge is unreliable as a sole gate**
  (position/verbosity/recency/self-preference bias; ~80% human agreement). Use it as a *layer*
  with expert-in-the-loop — which we do.
- **Provenance on every claim.** "Every claim traceable to a source" aligns with both the
  reliability literature and the legal mitigations (see §4).
- **Public-data-only, robots-respecting, no auth.** Legally the strongest posture (see §4).

## 2. Where the pipeline diverges (biggest gaps)

### 2.1 Single-channel discovery vs. the "data waterfall"
Commercial enrichment (Clay, Apollo, Clearbit, PeopleDataLabs) **cascades 75+ providers** in
priority order with conflict-resolution rules (prefer most-recent / most-authoritative source).
They report **80–95% coverage** vs **50–60%** for any single source. We are Firecrawl-only.

- **Implication:** our coverage ceiling is structurally lower than commercial tools'. That may be
  acceptable for a one-time alumni build, but it's the single largest architectural difference.
- **Cheap partial fix:** add 1–2 free/cheap channels (e.g. a structured search API, Wikipedia/
  Wikidata for notable alumni) as fallback when Firecrawl returns thin results — without paying
  for a full provider stack.

### 2.2 No deterministic entity-resolution pre-filter before the LLM gate
Record linkage is a mature discipline (Splink — probabilistic Fellegi-Sunter, free, scales to
millions; Zingg, dedupe.io — active-learning ML). Standard pattern: **blocking → fuzzy scoring →
LLM only on the ambiguous middle.**

- We currently send essentially every candidate to the **Sonnet** identity gate — the expensive
  step that drives the ~90%-Claude cost.
- **Fix:** add a cheap deterministic/fuzzy pre-filter on strong signals (full name + company +
  city + cohort/school). Strong exact matches auto-pass cheaply; only genuinely ambiguous cases
  reach Sonnet. Cuts cost *and* improves reliability.

## 3. Concrete cost-reducing changes (targeting the 90%-Claude problem)

Ranked by impact-per-effort:

1. **Anthropic Batch API — 50% discount.** 24h async SLA, ideal for a one-time 1,000-person
   build. Almost no code change beyond switching the call path. ~50% off all Claude spend.
2. **Prompt caching — cache reads at 0.1× input.** Our identity/structuring prompts share a
   large static prefix (instructions, schema, few-shot). Cache it. **Stacks with Batch** →
   combined ~70–80% effective reduction on input tokens.
3. **Native Citations API** instead of hand-rolled per-claim quote extraction. Grounds claims in
   source quotes natively: ~15% recall boost, source-hallucination ~10%→0%, and **you don't pay
   output tokens for quoted text.** Directly implements "every claim carries a source quote."
4. **ER pre-filter (§2.2)** — stop paying Sonnet on easy matches.

Combined, 1–4 plausibly take the Claude portion down enough to roughly halve total run cost.

## 4. Legal posture (summary of dedicated review)

- **CFAA risk LOW.** hiQ v. LinkedIn (9th Cir. 2022) and Meta v. Bright Data (Jan 2024):
  scraping logged-off public data is not "unauthorized access."
- **Real exposure is data-processing, not scraping.** GDPR/CCPA: "legality to scrape ≠ legality
  to process" — need lawful basis, opt-out, deletion path. Plus **wrong-person-merge defamation**
  risk.
- **Mitigations (mostly already in design):** retain provenance, manual review < 90% confidence,
  never auto-merge uncertain identities, semi-annual refresh, audit logs.

## 5. Context-handling note

Char-truncating each source to ~12k is the naive approach ("lost in the middle" / context rot).
Better: **rerank/select top passages and place the strongest source first.** Low priority vs. the
cost wins above, but worth doing when we revisit structuring quality.

## 6. Firecrawl endpoint notes

- `/search` can return **full page content** (not just snippets) in one call — potential to
  reduce separate `/scrape` credits. Worth a test when credits return.
- `/scrape` = 1 credit (markdown) / 5 credits (JSON-mode). `/extract` is deprecated → `/agent`.
