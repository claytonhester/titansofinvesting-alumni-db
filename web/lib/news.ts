import { curatedNews, type CuratedNewsRow } from "./db";
import { NEWS_CATEGORIES, type NewsCategory } from "./news-types";
import type { NewsItem, NewsFeedData } from "./news-types";

// The "In the news" feed reads the CURATED rows the Haiku news agent produces
// (category + one-line summary + importance per article). No mock fallback —
// the feed is empty until enrichment + curation have run, and the tab renders an
// honest empty state.
export type { NewsCategory, NewsItem, NewsFeedData } from "./news-types";
export { NEWS_CATEGORIES } from "./news-types";

const CATEGORY_SET = new Set<string>(NEWS_CATEGORIES);

function asCategory(value: string): NewsCategory {
  return (CATEGORY_SET.has(value) ? value : "Company News") as NewsCategory;
}

function toNewsItem(row: CuratedNewsRow, index: number): NewsItem {
  return {
    id: `${row.name_slug}-${index}`,
    nameSlug: row.name_slug,
    personName: row.full_name,
    school: row.school,
    titanClass: row.titan_class,
    category: asCategory(row.category),
    headline: row.headline,
    snippet: row.summary,
    sourceUrl: row.source_url,
    sourceHost: row.source_host,
    date: row.date,
    relevance: row.importance,
  };
}

export function getNewsFeed(limit = 40): NewsFeedData {
  const rows = curatedNews(limit);
  return { items: rows.map(toNewsItem), isSample: false };
}
