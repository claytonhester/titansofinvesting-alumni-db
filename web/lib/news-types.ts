export type NewsCategory =
  | "Funding & Deals"
  | "Leadership Moves"
  | "Market Views"
  | "Recognition"
  | "Company News";

export const NEWS_CATEGORIES: readonly NewsCategory[] = [
  "Funding & Deals",
  "Leadership Moves",
  "Market Views",
  "Recognition",
  "Company News",
] as const;

export interface NewsItem {
  id: string;
  nameSlug: string;
  personName: string;
  school: string;
  titanClass: number;
  category: NewsCategory;
  headline: string;
  snippet: string;
  sourceUrl: string;
  sourceHost: string;
  date: string;
  relevance: number;
}

export interface NewsFeedData {
  items: NewsItem[];
  isSample: boolean;
}
