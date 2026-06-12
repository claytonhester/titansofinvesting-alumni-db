# Titans of Investing — Alumni Intelligence

A searchable, analytical directory of **Titans of Investing** alumni — assembled
from the program's public class directory and enriched with source-attributed,
publicly available career data. It answers questions a static alumni list can't:
*where do Titans start, how far do they climb, where do they cluster, and who
should I talk to at firm X?*

The project is two halves that share one SQLite database:

| Half | What it does | Stack | Lives in |
|---|---|---|---|
| **Pipeline** | Collects + enriches alumni into a queryable DB | Python · Claude (Haiku/Sonnet) · PDL · Perplexity | [`pipeline/`](pipeline/) |
| **Web app** | Serves the directory, insights, and a chat interface | Next.js · TypeScript · SQLite | [`web/`](web/) |

---

## Data & privacy

The starting roster comes from the **public** Titans of Investing class
directory. Enrichment adds only **publicly available, source-attributed**
career facts (each claim stores its source URL and a verbatim quote so a human
can verify it). The app is **read-only**. No private or behind-login data is
collected, and the pipeline is built to refuse low-confidence identity matches
rather than guess.

If you are an alum and want a correction or removal, open an issue.

---

## How it works

```
Public class directory
        │
        ▼
  Stage 1 · collect      →  people (SQLite)
        │
        ▼
  Stage 2 · enrich       →  claims  (identity-gated; PDL résumé spine +
   (per person)              Perplexity news + LinkedIn, all source-attributed)
        │
        ▼
  Stage 3 · classify     →  per-person insights: sector, cross-industry
   + roll up                 seniority rung, career velocity, full trajectory,
                             and the cohort KPIs/charts
        │
        ▼
  sync-db                →  Next.js web app reads a bundled read-only snapshot
```

1. **Collect** — parse the public directory into a `people` table.
2. **Enrich** — for each person, gather career history, current role, education,
   and news behind a strict identity gate; every fact lands in `claims` with its
   source. The People-Data-Labs match is the résumé "spine"; Perplexity and
   LinkedIn fill gaps.
3. **Classify** — derive per-person insights: sector, a cross-industry seniority
   rung (Entry → Manager → Senior Leadership → Executive), career velocity, and
   the position-by-position trajectory.
4. **Roll up** — aggregate the cohort into the Overview's KPIs and charts.
5. **Serve** — the web app reads a bundled snapshot of the DB (read-only).

See [`RUNBOOK.md`](RUNBOOK.md) for the exact commands to run each stage.

---

## Repository structure

```
.
├── pipeline/        # Python: collection, enrichment, classification, roll-ups
│   ├── data/        # the SQLite DB + working datasets
│   ├── tests/       # pytest suite for the pure logic
│   └── *.py         # one module per concern (collect, enrich, reconcile, KPIs…)
├── web/             # Next.js app: directory, insights, chat
│   ├── app/         # routes + views (Overview, Directory, person pages)
│   ├── lib/         # DB access, insights, chat gate
│   └── data/        # bundled read-only DB snapshot the app ships with
├── docs/            # design notes + plans (architecture, enrichment, insights)
└── RUNBOOK.md       # operator's guide: run the pipeline end to end
```

---

## Running it

**Web app (local):**

```bash
cd web
npm install
npm run dev        # predev syncs the DB snapshot automatically
```

**Pipeline:** requires API keys (see [`.env.example`](.env.example)). Full
sequence is documented in [`RUNBOOK.md`](RUNBOOK.md); the short version:

```bash
cd pipeline
python phase2_enrich.py      # enrich a batch
./finalize_pass.sh           # sectors → completeness → seniority → snapshot → sync
```

---

## Tech notes

- **Identity-first enrichment.** A claim is only kept if it's anchored to a
  verified identity; the pipeline fails closed on weak matches.
- **Source attribution everywhere.** Every claim carries a URL + quote.
- **Cross-industry seniority.** Titles are classified per sector (a finance "VP"
  and a corporate "VP" are different rungs), cached so re-runs are near-free.
- **Cost-aware model routing.** Cheap deterministic passes do the bulk; Claude
  is used only where judgment is needed, with per-run cost logging.

---

*Built from the public Titans of Investing class directory. Read-only. The
published directory is the source of record.*
