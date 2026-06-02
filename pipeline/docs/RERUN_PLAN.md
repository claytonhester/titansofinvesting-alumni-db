# "Press a Button to Run It Again" — Operational Re-run Plan

Goal: let a non-engineer (the boss) kick off enrichment — the first build, or a
later refresh — without touching code, and know what it will cost before it runs.

This document describes (1) what exists today, (2) the one small piece to build
for a true "refresh everyone" button, and (3) the guardrails that keep a
button-press safe and cheap to reason about.

---

## What the pipeline does today

`python phase2_enrich.py --limit N` enriches the **next N people who are not yet
done**. It is already:

- **Resumable** — each person is committed before the next starts, and
  `pending_people()` skips anyone whose structuring phase is already `done`. A
  crash (or a credit-out) mid-batch loses nothing; re-running picks up where it
  stopped.
- **Cost-logged** — every run appends one row to `data/cost_log.jsonl` with the
  measured Firecrawl credit delta and real Claude token spend.
- **Safe on identity** — only auto-accepted sources (confidence ≥ 0.85) feed
  claims; uncertain matches go to a review queue, never an auto-merge.

So **two of the three modes a boss needs already work**:

| Mode | How | Status |
|---|---|---|
| **Build new** (enrich people we've never touched) | `--limit N` (or no limit for all remaining) | ✅ works |
| **Resume** (finish an interrupted batch) | re-run the same command | ✅ works |
| **Refresh all** (re-enrich people already done, to pick up new web info) | — | ⚠️ needs the small piece below |

---

## The one piece to build: a "refresh" switch

Because `pending_people()` skips anyone marked `done`, re-running today will
**not** revisit completed profiles — it correctly avoids paying twice. To
*intentionally* refresh everyone, we need a flag that resets the relevant
people's batch status back to pending before the run (the `replace_*` writes are
already idempotent, so re-enriching simply overwrites the prior profile/claims).

Concretely, add to `phase2_enrich.py`:

- `--refresh` — reset all (or a `--since DATE` subset) people to pending, then run.
- Keep the default behavior unchanged (build-new/resume), so the safe path stays
  the default and "re-do everything" is an explicit, deliberate choice.

This is a small, well-scoped change (one new flag + one status-reset query). It
is the only code work standing between today and a true refresh button.

---

## The button itself (wrapping the CLI)

The boss should never see a terminal. Wrap the command in one of:

1. **A one-line script** (`run_enrichment.sh`) with a friendly prompt:
   *"Build new profiles" / "Refresh everyone" / "How many?"* → calls the right
   `phase2_enrich.py` invocation. Cheapest to ship.
2. **A small admin page** in the existing `web/` Next.js app: a button that
   triggers the run server-side and streams progress. Nicer, more work.

Either way the button maps to one of the three modes above.

---

## Guardrails to attach to the button (so a press is never scary)

1. **Pre-flight credit + budget check.** Before launching, call
   `remaining_credits()` and show: *"This will process ~N people, cost ≈ $X
   (Claude) + ~$Y (Firecrawl). You have Z Firecrawl credits — enough / NOT
   enough."* Refuse to start a run the credit balance can't finish. (See
   `COST_ANALYSIS.md` for the per-person numbers behind the estimate.)
2. **Confirm destructive refresh.** `--refresh` overwrites existing profiles, so
   the button must require an explicit confirm for that mode only.
3. **Show the receipt after.** Surface the just-written `cost_log.jsonl` row:
   actual people processed, measured Firecrawl $, Claude $, total. The boss sees
   exactly what the press cost.
4. **Point at the review queue.** A run produces auto-accepted profiles *and* a
   set of uncertain identities held for human review. The button's "done" screen
   should link to that queue — enrichment isn't "finished" until a human clears
   reviews.

---

## Recommended rollout

1. Land the `--refresh` flag (the only missing code).
2. Ship the one-line script (mode 1) with the pre-flight credit check — fastest
   path to a real button.
3. Once a billed run validates the cost-log numbers against this estimate,
   optionally graduate to the in-app admin page (mode 2).

> Note: live runs are currently blocked — Firecrawl credits are at 0 until the
> plan resets or we top up. The first billed batch will both validate
> `COST_ANALYSIS.md` and exercise this button end-to-end.

---

## Billed-validation gate (what the first paid run must check)

Two cost levers from `RESEARCH_FINDINGS.md` are **live now and offline-safe**;
two more are **deferred until a billed smoke test** confirms response parsing.
Do not flip the deferred two on blind — a malformed parse on a 1,000-person run
is expensive and hard to unwind.

| Lever | State | First-run check |
|---|---|---|
| **ER pre-filter** (`identity_prefilter.prefilter`) | ✅ live | Confirm `{n} by pre-filter` accepts are correct people (spot-check a few accepted-without-Sonnet sources); confirm nothing is wrongly auto-merged. |
| **Prompt caching** (`cache_control` on identity/structuring system prefix) | ✅ live | Confirm `usage.cache_read_input_tokens` > 0 after call #2; then extend `cost_log.py` to price cached input at 0.1× (today it bills cache reads at full rate → estimate is conservative/high). |
| **Anthropic Batch API** (50% off) | ⛔ deferred | Validate the async submit/poll/result path returns the same JSON shape `resolve_identity`/`structure_profile` already parse, on a **≤5-person** batch, before switching the call path. |
| **Citations API** | ⛔ deferred | Validate citation blocks map cleanly onto the `{value, source_url, quote}` claim grain on a ≤5-person batch before replacing hand-rolled quote extraction. |

**Gate rule:** a deferred lever graduates to "live" only after its ≤5-person
smoke test passes and the parsed output matches the current schema. Each is an
isolated, reversible switch (the call path, not the data model), so they can be
adopted one at a time.
