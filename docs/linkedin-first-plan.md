# LinkedIn-First Enrichment — Plan of Record

*Decided 2026-06-11. Supersedes the "LinkedIn as deep-path add-on" posture.
Context: Firecrawl plan upgraded to 100k credits/month; the Bart Howe case
(stale titles/dates invisible until compared against LinkedIn) proved the
gap-gated approach under-delivers.*

## Decision

LinkedIn becomes the **starting point** of enrichment for every person. The
existing web-discovery spine is demoted to the **fallback** for people whose
LinkedIn can't be found or verified — it is not removed or weakened.

## Per-person flow

```
1. HUNT      Firecrawl agent searches for the LinkedIn profile
             (anchors: name, school, class year, roster first-employer, city)
2. VERIFY    Claude verifier interrogates the returned profile against the
             roster anchors (education era + roster employer in early career).
             verdicts: verified / rejected / review
3a. VERIFIED LinkedIn = career spine (titles, dates, education, current role)
             -> PDL anchored on LinkedIn-confirmed employer
             -> web/news pass searches "name + confirmed employer"
             -> reconciler merges (dated/recent wins; LinkedIn default for
                titles+dates, PDL for firmographics, web for bios/news)
3b. MISS     Fall back to today's pipeline unchanged:
             web discovery -> identity gate -> structuring -> PDL (roster anchor)
             -> IF web verifies an employer: ONE warm LinkedIn retry with it
             -> still nothing: honest blank + flagged for future re-runs
```

**Invariant (the Ricardo rule):** no source family ever writes claims without
identity verification. LinkedIn agent output gets the same Haiku gate PDL
already has. The shared broker/SEO-echo blocklist applies to every ingestion
path including Sonar news (pending task).

## Build list (Phase 0 — code, no spend)

| # | Item | Notes |
|---|------|-------|
| 1 | `linkedin_verify.py` — roster-anchor verifier for agent output | the keystone; same bar as `verify_pdl_claims` |
| 2 | `linkedin_refresh.py` — append-only runner for already-enriched people | agent -> verify -> append claims -> re-reconcile -> recompute insights; NEVER replace |
| 3 | Reconciler hierarchy rule | dated/recent wins; LinkedIn default for titles+dates |
| 4 | PDL warm-anchor retry | if PDL missed and LinkedIn verified an employer, retry once |
| 5 | Unified research-policy flag (`bulk` / `deep` / `refresh`) | replaces the four independent gates' criteria |
| 6 | Shared domain blocklist incl. Sonar news path | already a pending task (wwana.com incident) |
| 7 | Profile-completeness score in `finalize_pass.sh` + Build Status | makes future audits automatic |

## Phase 1 — Paid pilot: the 49-person sweep

Targets: `pipeline/data/linkedin_sweep_targets.json` (audited 2026-06-11 —
people with undated/untitled/companyless career entries or no career history).
Estimated spend: ~2,500–4,000 credits + ~$3 Claude. DB backup before run.

## Phase 2 — Go/no-go gate (measured, not assumed)

Proceed to the full cohort only if the pilot shows:

- **Avg agent cost ≤ 80 credits/person** (estimate is 50; 80 is the ceiling at
  which the 1,200-run still fits ~2 months of the 100k plan)
- **Verified-find rate ≥ 60%** on this worst-case population (expect higher on
  the general population)
- **Zero namesake leaks** on a hand-check of 10 random enriched results
  (Bart/Payal-style side-by-side against the live LinkedIn)
- **Review-queue rate ≤ 25%** (above that, the verifier prompt needs tuning
  before scale)

## Phase 3 — Full cohort (~1,100 remaining)

School/class batches of ~50 with `--max-credits` caps; `finalize_pass.sh`
after each batch; commit web DB per batch. PDL per monthly quota (see below).

## Cost of record (full ~1,200)

| Component | Estimate |
|---|---|
| Firecrawl (agent ~50cr + deep/news ~20cr per person) | ~84k credits (~1 plan-month; 2 for headroom) |
| PDL (~65% match × $0.28) | ~$210–240 |
| Claude (identity/extract/verify/reconcile/KPI) | ~$60–70 |
| Perplexity (discovery + news) | ~$25 |
| **Cash total** | **≈ $300–350** |

## Open items (the two non-code blockers)

1. **PDL quota procurement** — current cycle exhausted (API 402 despite
   dashboard showing 3). Full run needs ~800 matches: either a paid PDL tier
   or splitting across July/August resets. Deferred queue: Xuan Yong,
   Phoebe Lin, Alejandra Chavira first.
2. **Human review cadence** — verifier "review" verdicts need an owner.
   Proposal: surface count in Build Status; operator clears the queue before
   each batch ships.

## Standing safety rails

- DB backup before every paid run; append-only for already-enriched people.
- Per-run `--max-credits` ceilings; cost_log entry per batch.
- Ricardo Lopez–class people re-enter only via verified anchors, never bare
  name+school searches (PDL or agent alike).
- Honest blanks stay blank: "not found" is a per-run verdict, never a claim.
