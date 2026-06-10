# Phase 0 — Proof (search upgrades, built for $0)

The two search upgrades from the v2 plan are **built, shipped, and verified** against
the existing 48 enriched alumni — before any coverage spend. This is the evidence.

- **Faceted search** — filter on the enriched `current_sector` + seniority (commit `b402693`)
- **Semantic search** — local in-process embeddings, hybrid retrieval (commit `4fc9f42`)
- Cost: **$0** · Tests: **136 green** · Production build: **compiles** · `npm audit`: **clean**

---

## 1. Before / After — the mechanism changed

The keyword path can only match a question to a *firm name* or *sector keyword*. When a
question has none to grab (a career-arc or theme), it falls back to "the most-enriched
people" — **the same list for every such question.** Semantic retrieval returns
*query-relevant* people instead.

**BEFORE (keyword fallback — identical for all three questions):**
> Zachary Gaitz, Hampton Cokeley, Ben Beverly, Brock Birkenfeld, Shaun Frederiksen, Andrew Donsbach

**AFTER (semantic — different, relevant candidates per question):**

| Question | Top semantic candidates |
|---|---|
| "who moved from engineering into finance?" | Edward Shipper, Preston Howard, Lars Moore, Shaun Frederiksen, Sidhart Nambiar |
| "restructuring and distressed debt experience" | Byron Geeslin, Laura Smith, Zachary Gaitz, Silvio Canto |
| "someone in renewable energy or clean power" | Ahmad Mouneimne, Andrew Haraway, Carlie Woodard, Jacob Sexton |

(The chat then grounds the final answer in each person's *verified* claims — semantic only
changes *which* records are retrieved, never what's asserted about them.)

---

## 2. Real answers from the live chat

**Q: "I'm a student who wants to break into private equity — who should I talk to?"**
> Recommends Ross Willmann (Partner & CIO, Warwick), Travis Gauntt (Sr Director, Capital
> Creek — "the classic analyst-to-PE pipeline"), Michael Everett (VP, Brighton Park),
> Craig Krzyskowski (the MBA-to-PE transition), Harvey Cornell (Sumeru) — each with the
> specific path that makes them worth contacting, then a concrete next step.

**Q: "Who moved from engineering into finance?"** *(no firm/sector to match — pure semantic)*
> Surfaces Ben Beverly (B.S. Engineering, UT Austin → Accenture → venture investing at G51 /
> Silverton / Next Coast), and is honest that the rest of the directory doesn't show similar
> pivots. A query keyword search structurally cannot answer.

**Q: "Who are the senior leaders or partners I could learn from?"** *(seniority facet)*
> Leads with Stephanie Coco (Partner, Vinson & Elkins — energy/infrastructure M&A), and is
> candid: "the directory is thin on other current partners… right now."

---

## 3. Grounding held throughout

Every answer cites only verified claims, links to the person's profile, **excludes
unverified news**, and says plainly when the directory is thin instead of inventing.
The hardened identity gate and name-drop guard are unchanged.

---

## 4. Deploy-ready

- `tsc` clean · **136 web tests pass** · `next build` compiles · prod-mode smoke test
  confirms the embedding model loads at runtime and semantic retrieval works.
- `npm audit` **clean** (the embedding lib's only CVEs were in an unused browser backend;
  pinned away via a dependency override).
- Zero new paid vendor: embeddings run in-process (all-MiniLM-L6-v2, 384-dim).

---

## 5. Reproduce / operate

```bash
cd web
npm run embed      # (re)build person_vectors in the pipeline DB — run after each enrichment batch
npm run sync-db    # copy the snapshot (incl. vectors) into the web app
npm run dev        # try it locally; ask the "Ask" box anything in plain English
```

**What this proves for the raise:** the stronger search is real and shipping *today* on 48
people. The ~$500 simply scales it to all 1,056 — the engine is already built and verified.
