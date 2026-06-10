# Titans of Investing — Enrichment & Search v2 Plan

**Goal:** enrich the remaining ~1,008 alumni *and* make search materially stronger
(semantic + faceted retrieval, higher-fidelity data), within a **~$500** budget.

**Status quo (verified from the cost log + DB, 2026-06-10):**
- 48 of 1,056 alumni enriched. Marginal cost observed: **~$0.26/person all-in**
  (Firecrawl + Perplexity/Sonar + Claude + PDL).
- Search today is **keyword SQL only** — matches `initial_company` and (as of this
  week) `current_employer`. No semantic retrieval; structured facets exist in the DB
  but are unused by search.
- Grounding is hardened: identity gates, the name-drop guard, verified-claims-only
  synthesis, off-topic rejection. All of that is preserved here.

---

## The $500, allocated

| Line | What | Cost |
|---|---|---|
| Baseline enrichment — all 1,008 remaining | existing pipeline, Jina-first discovery | **~$265** |
| Gated deep enrichment — ~500 with signal | Sonnet/Opus identity, extra scrapes, reconciliation | **~$200** |
| Embeddings (one-time, all 1,056) | semantic index | **~$5** |
| Query-time embedding + rerank | negligible per query | ~$0 |
| Contingency (~6%) | retries, re-embeds | **~$30** |
| **Total** | | **~$500** |

Two of the three search upgrades (semantic, facets) are **nearly free in API** — the
money buys *coverage* and *fidelity*; the build buys *stronger retrieval*.

---

## Workstream A — Coverage + gated deep enrichment  *(where the dollars go)*

Run the existing per-school enrichment (`pipeline/phase2_enrich.py`) over the 1,008
un-enriched people, with two upgrades:

**A1. A deep-enrichment gate (budget discipline).**
After baseline discovery, score the signal (confident LinkedIn/PDL match found?
≥N quality sources? a current-employer claim resolved?). 
- **High signal →** escalate: Sonnet identity verification (fewer wrong-person
  merges — the namesake failure mode), extra Firecrawl scrapes, a Sonnet
  reconciliation pass to merge multi-source claims.
- **Low signal →** stop at baseline. A junior alum with no web presence does not get
  richer at $0.50 than at $0.26 — don't spend there.
- Files: `phase2_enrich.py` (gate + orchestration), `identity.py` (Sonnet/Opus
  escalation routing — the tiering already exists in the model-routing design),
  `reconcile.py` (Sonnet merge pass), `cost_log.py` (meter the deep path separately).

**A2. Raise the PDL match rate (~60% today).**
Pass more disambiguation anchors to PDL (`pdl_enrich.py` — `school` already added;
add `grad_year`, `city`, middle name). Every extra match = free firmographics +
current employer, which is what powers "who works at X now."

**A3. Stretch the budget — Jina for the masses, Firecrawl for the few.**
`http_fetch.py` (Jina, free) is the default fetcher we built this session. Run
baseline discovery on Jina to avoid the Firecrawl-credit bottleneck (a full
Firecrawl run would need ~110k credits; 230 remain). Reserve **Firecrawl credits for
the high-signal deep path only** — the people worth the stronger fetch.

---

## Workstream B — Semantic search  *(biggest retrieval upgrade; ~free in API)*

Today "who can advise on climate-tech investing" returns nothing unless an employer
*string* contains those words. Fix: retrieve on *meaning*.

**B1. Embed each person.** Compose a profile string (name + current role + career
history + skills + education + bio) and embed it. Provider: Voyage AI or OpenAI
embeddings (~$0.05 for all 1,056). New dependency: one embeddings API key.
- New file: `pipeline/embed_people.py` — batch-embed, store vectors as a BLOB column
  on `people` (or a `person_vectors` sidecar table). Re-run after each enrichment
  batch (cheap).

**B2. Hybrid retrieval at query time.** Embed the visitor's question; brute-force
cosine over ~1,056 vectors in Node (microseconds at this scale — **no vector DB
needed**). Union the semantic top-K with the existing keyword hits, dedupe, rank, and
feed the same grounded synthesis.
- Files: new `web/lib/chat/semantic.ts` (cosine rank), `web/lib/chat/search.ts`
  (hybrid union), `web/lib/db.ts` (load vectors). Synthesis/grounding unchanged.

---

## Workstream C — Structured facet search  *(data already exists; pure plumbing)*

`person_insights` already stores `current_industry`, `job_function`,
`pdl_seniority`, `current_sector`, `years_experience` (free from PDL matches). Skills
live as `claim_type='skill'`. None of it is searchable yet.

- Index those columns; extend the planner (`web/lib/chat/plan.ts`) to emit
  `industry` / `seniority` / `function` params; extend `searchPeople`
  (`web/lib/db.ts`) to filter via a `JOIN person_insights` + a skill-keyword
  subquery. Then "senior PE professionals in energy" is a precise query, not keyword
  soup.

---

## Sequencing (de-risks the spend)

**Phase 0 — build the search layer first, on the existing 48 (cost: ~$5, mostly build).**
Ship B + C against the *current* data. Prove semantic + faceted search works and is
better before spending a dollar on coverage. This is the key risk-reducer: you see
the stronger product for ~free.

**Phase 1 — run gated deep enrichment over the 1,008 (cost: ~$465).**
Dry-run + cost cap + DB backup (all already built into the runners). Meter the deep
path separately so we can confirm the gate is working.

**Phase 2 — re-embed, refresh the snapshot, deploy (cost: ~$5).**
`npm run sync-db`, regenerate vectors, ship.

---

## Validation & guardrails (all already in place)

- Dry-run-by-default runners, hard `--max-usd` cap, automatic DB backup, append-only
  writes, cost logging.
- Identity gate + name-drop guard + verified-claims-only grounding — unchanged.
- A/B the search: a fixed question set, keyword-only vs hybrid, eyeball the lift.
- Gate the deep path on signal so budget flows to people it can actually improve.

## Risks

| Risk | Mitigation |
|---|---|
| Embeddings add a new provider dependency | Voyage (cheap, strong) or a local model; ~$0.05 total |
| PDL match rate plateaus for low-profile people | the deep gate stops spending where there's no signal |
| Firecrawl credit bottleneck (~110k needed, 230 left) | Jina for baseline; Firecrawl only for high-signal deep path |
| Wrong-person merges erode search trust | Sonnet/Opus escalation on ambiguous identities is the main thing the $ buys |
| Scope creep | Phase 0 proves the search lift for ~$0 before any coverage spend |

---

## One-line summary for the funder

> **$500 turns a 48-person keyword directory into a ~1,056-person directory with
> semantic + faceted search and higher-fidelity, identity-verified data.** ~$265
> enriches everyone; ~$200 buys depth where there's signal; the search upgrades are
> a build, not a spend; and we prove the stronger search for ~$0 before committing
> the coverage budget.
