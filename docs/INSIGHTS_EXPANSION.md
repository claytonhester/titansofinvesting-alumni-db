# Insights Expansion — design spec

Goal: capture the data already within reach (PDL extras we discard + zero-cost
derivations) and wire it through the whole system — per-person profile, cohort
Overview, and the grounded chat — without breaking the "never assert what we
haven't verified" rule or spending materially more per person.

Two kinds of new data:
- **Collected** — fields already returned by the PDL match we pay for, plus
  skills/certs. No new API spend; just stop discarding them.
- **Derived** — computed from data we already store (claims + person_insights +
  grad_year). Zero collection cost; pure functions.

---

## 1. The three storage layers (unchanged shape, extended contents)

| Layer | Grain | Holds | Gets |
|---|---|---|---|
| `claims` | one fact / person | verifiable résumé facts w/ provenance | + `skill`, `certification` |
| `person_insights` | one row / person | derived attrs + KPI flags | + collected attrs + derived metrics |
| `insights_snapshot` | one row / year | cohort aggregate (JSON payload) | + landing sectors, migration, averages, clusters |

Rule of thumb used below:
- **Multi-valued, renderable, wants provenance →** `claims` (skills, certs).
- **Single-valued per-person attribute or metric →** `person_insights`.
- **Cohort math the narrative/Overview needs →** `insights_snapshot` (computed in
  phase3, single source of truth; web renders).
- **Dynamic membership the chat needs →** live web query (clusters), not snapshot.

---

## 2. Field-by-field: where each piece goes

### Collected from the PDL match (already paid for)

| Field | PDL source (verify on 1st real resp) | Storage | Surfaced |
|---|---|---|---|
| Current employer industry | `job_company_industry` | `person_insights.current_industry` | person page tag; landing-sector aggregate |
| Company size | `job_company_size` | `person_insights.current_company_size` | person page tag |
| Job function / role | `job_title_role` / `job_title_sub_role` | `person_insights.job_function` | cross-check buy-side; person page |
| Seniority level | `job_title_levels[]` | `person_insights.pdl_seniority` | cross-check MD+; not shown raw |
| Current-role start | `job_start_date` (year) | `person_insights.current_role_start_year` | → derived tenure |
| Total years experience | `inferred_years_experience` | `person_insights.years_experience` | person page stat; cohort avg |
| Skills | `skills[]` (cap ~12) | `claims` type `skill` | person page chips |
| Certifications | `certifications[]` | `claims` type `certification` | person page badges (CFA/CPA); cohort count |
| LinkedIn connections | `linkedin_connections` | `person_insights.linkedin_connections` | person page (rough network size) |

Mapping is **defensive**: any field PDL omits is simply skipped — never blocks the
row. Field names confirmed against the first real PDL response before relying on them.

### Derived (computed, zero collection)

| Metric | Computed from | Storage |
|---|---|---|
| Tenure at current firm | snapshot_year − current_role_start_year | `person_insights.tenure_years` |
| Career velocity (yrs to MD) | grad_year → earliest MD+ role start | `person_insights.years_to_md` (null if not MD) |
| Job mobility | distinct employers in career_history | `person_insights.num_employers` |
| Advanced degree | education claims beyond undergrad (MBA/JD/MS/PhD) | `person_insights.has_advanced_degree` |
| Current sector | classifySector(current_employer) | `person_insights.current_sector` |
| Left Texas | roster city in TX vs current location | `person_insights.left_texas` |
| In-the-news count | count of news_mention claims | computed on read (cheap) |

### Aggregates (phase3 → snapshot payload)

- `landing_sectors` — breakdown of `current_sector` (mirror of first-job sectors).
- `migration` — % still in Texas, top destination cities (already have both ends).
- `advanced_degree_rate`, `avg_tenure`, `avg_years_to_md`, `avg_num_employers`.
- `firm_clusters` — top current employers by Titan headcount (panel).
- (chat) `cohortAtFirm(firm)` / `cohortInCity(city)` — **live** web queries, not
  snapshot, because the chat needs the member list, not just a count.

---

## 3. A python sector classifier (new, mirrors the web)

Landing sectors + current_sector are computed in **python** (phase3 + classify
step) so the snapshot is the single source of truth. We already mirror
`normalize` py↔ts; add `sector_classify.py` mirroring web `classifySector` +
`SECTOR_RULES`. One keyword table, kept in sync, used for first-job AND landing
sectors so both agree.

(Origins first-job sectors, currently computed live in the web, move to read the
python-computed value for consistency — or stay web-live; decision below.)

---

## 4. Surfacing — where the user sees it

### Person page (`app/person/[slug]`)
- Header: current employer gains **industry + size** tags, **tenure** ("3 yrs"),
  **years experience**.
- New **Credentials** row: certification badges (CFA, CPA…), skill chips.
- New **network hook**: "N other Titans at {employer}" → links to a filtered
  directory view. This is the single highest-value add for the app's purpose.

### Overview & Insights
- **Outcomes tab** gains a **Landing sectors** panel (twin of Origins sectors)
  and the existing firms list.
- **New "Where Titans cluster"** panel: top firms by member count (networking).
- **Scorecard / second stat strip**: advanced-degree rate, avg time-to-MD,
  % left Texas, avg tenure — measured, with empty states until enriched.
- **Map** already current-location; migration % is its aggregate.
- **Narrative** is fed every new number so the prose reflects them.

### Chat / search (`app/api/chat`, `lib/db.ts searchPeople`)
- New typed filters: `sector` (landing), `certification`, `seniority`,
  `industry`. Lets the planner answer "who has a CFA in Houston PE", "who else is
  at Goldman" (via cohortAtFirm), "which Titans went buy-side from banking".
- This is where the collected data pays off most for the user's actual workflow.

### Build status
- Coverage card optionally notes which enriched fields are populated. Minor.

---

## 5. What changes — file by file

Pipeline:
- `pdl_enrich.py` — return richer result: skill/cert claims + an attrs dict
  (industry, size, function, pdl_seniority, role_start_year, years_exp,
  connections). Defensive getters.
- `sector_classify.py` *(new)* — python mirror of web classifySector.
- `career_analysis.py` — add `num_employers`, `years_to_md`, `tenure` helpers.
- `education_analysis.py` *(new or in career_analysis)* — `has_advanced_degree`.
- `person_insights_store.py` — new columns + upsert/read.
- `phase2_enrich.py` — thread PDL attrs + compute derived metrics into the
  person_insights write; add skill/cert claims to the claim set.
- `insights_rollup.py` / `kpi_rollup.py` / `phase3_insights.py` — new aggregates
  into the snapshot payload; feed narrative.
- `insights_store.py` — extend `InsightsSnapshot` + payload (typed).

Web:
- `lib/db.ts` — `InsightsSnapshot` type additions; `cohortAtFirm`,
  `cohortInCity`, firm-cluster query; new `searchPeople` params.
- `lib/insights.ts` — surface new aggregates + per-person attrs.
- `app/insights-views.tsx` — landing-sectors panel, clusters panel, new stat
  strip, empty states.
- `app/person/[slug]/page.tsx` + `lib/resume.ts` — render industry/size/tenure/
  years-exp, certifications, skills, "N other Titans here".
- `app/api/chat/route.ts` + chat tools — new filters; cluster lookups.

Tests: pure-function tests for every derivation (velocity, mobility, advanced
degree, sector, migration, tenure), store round-trips, rollup math, web queries.

---

## 6. Cost impact

Effectively flat. The PDL extras ride the match we already buy. Skills/certs are
in the same response. Derivations are free. The only possible add is letting
Haiku sanity-check job_function/seniority against our own calls — optional; the
deterministic mapping from PDL's own fields is probably enough. So per-person
cost stays ~the same as today.

---

## 7. Build phasing (each phase ships green, no run required to build)

1. **Collect** — pdl_enrich extras + skill/cert claims + person_insights columns
   + sector_classify. (Unlocks everything downstream.)
2. **Derive** — velocity, mobility, advanced-degree, tenure, current_sector,
   left_texas in the phase2 classify step.
3. **Aggregate** — phase3 snapshot additions + narrative.
4. **Surface — Overview** — landing sectors, clusters, stat strip.
5. **Surface — person page** — tags, credentials, network hook.
6. **Surface — chat** — new filters + cohort lookups.

Run the 5-person test only after phase 1–3 land, so the test exercises the full
collection + derivation path.

---

## 8. Open decisions (need a call)

- **D1. person_insights width vs split.** Keep one wide row (simplest, ships
  fast) or split collected-attrs vs derived-metrics into two tables (cleaner
  provenance). Recommendation: one wide row, clearly sectioned.
- **D2. Sector classification home.** Mirror into python (snapshot is source of
  truth, consistent) vs keep sectors web-live. Recommendation: mirror to python.
- **D3. Scorecard real estate.** The 4 KPIs are the headline. Put the new stats
  (degree rate, time-to-MD, left-Texas, tenure) in a *second* strip below, not
  mixed into the 4. Recommendation: second strip.
- **D4. Skills volume.** Cap skills at ~10–12 to avoid noise. Certs uncapped.
- **D5. PDL field availability.** Confirm exact field names + presence on the
  first real PDL response before trusting them (linkedin_connections especially
  is plan-dependent). Defensive mapping regardless.
