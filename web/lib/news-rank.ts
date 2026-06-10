import type { NewsCategory, NewsItem } from "./news-types";

// Editorial weight per category for the "All" feed. In a thin-coverage directory
// (<10% enriched), Recognition — a Forbes 30 Under 30, a named award/ranking — is
// the rarest, highest-signal thing we can surface, so it leads. Routine Leadership
// Moves (a hire or promotion) are common and freshly-dated, so sorting by recency
// (the old behavior) buried recognition under them. We blend the category weight
// with the item's own importance so a genuinely MAJOR move or deal still rises,
// but the default order is recognition-first.
export const CATEGORY_RANK: Record<NewsCategory, number> = {
  Recognition: 1.0,
  "Funding & Deals": 0.65,
  "Market Views": 0.45,
  "Leadership Moves": 0.3,
  "Company News": 0,
};

// importance (relevance) is 0..1, so the blended score lives in roughly 0.3..2.0.
// A real Recognition (importance >= ~0.5) lands >= 1.5 and stays above any
// Leadership Move (max 1.3) — but a high-importance move/deal clears the weaker
// Market Views and low recognitions, which is the "a few important ones" the
// editor wants near the top.
export function editorialScore(
  item: Pick<NewsItem, "category" | "relevance">,
): number {
  return (CATEGORY_RANK[item.category] ?? 0) + (item.relevance ?? 0);
}

// "All" feed order: highest editorial score first; ties broken by recency, then
// raw importance. Pure function so it's unit-tested directly.
export function byEditorialRank(a: NewsItem, b: NewsItem): number {
  const d = editorialScore(b) - editorialScore(a);
  if (Math.abs(d) > 1e-9) return d;
  if (a.date && b.date && a.date !== b.date) return a.date < b.date ? 1 : -1;
  return b.relevance - a.relevance;
}
