import { recentNews, type NewsMention } from "./db";
import type { NewsItem, NewsFeedData } from "./news-types";

// The vetting pipeline (co-mention filter + LLM identity/category gate) will write
// these onto each surviving news_mention claim before it ever reaches this layer.
// Until GNews + vetting have run, the feed is seeded with clearly-labeled sample
// articles so the design can be reviewed. `isSample` drives the preview banner.
export type { NewsCategory, NewsItem, NewsFeedData } from "./news-types";
export { NEWS_CATEGORIES } from "./news-types";

const NEWS_DATE_SEP = " — ";

function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return "";
  }
}

function splitNews(value: string): { date: string; headline: string } {
  const idx = value.indexOf(NEWS_DATE_SEP);
  if (idx > 0 && /^\d{4}-\d{2}-\d{2}$/.test(value.slice(0, idx))) {
    return {
      date: value.slice(0, idx),
      headline: value.slice(idx + NEWS_DATE_SEP.length),
    };
  }
  return { date: "", headline: value };
}

// Until the vetting pipeline persists a real category/relevance, default real
// rows into a neutral bucket so they still render in the news-style layout.
function toNewsItem(mention: NewsMention, index: number): NewsItem {
  const { date, headline } = splitNews(mention.value);
  return {
    id: `${mention.name_slug}-${index}`,
    nameSlug: mention.name_slug,
    personName: mention.full_name,
    school: mention.school,
    titanClass: mention.titan_class,
    category: "Company News",
    headline,
    snippet: mention.quote,
    sourceUrl: mention.source_url,
    sourceHost: hostOf(mention.source_url),
    date,
    relevance: 0,
  };
}

// Reads vetted news_mention claims; falls back to the seeded preview set when the
// pipeline hasn't produced any yet (so the tab is never an empty void in dev).
export function getNewsFeed(limit = 40): NewsFeedData {
  const real = recentNews(limit);
  if (real.length > 0) {
    return { items: real.map(toNewsItem), isSample: false };
  }
  return { items: SAMPLE_NEWS, isSample: true };
}

const SAMPLE_NEWS: NewsItem[] = [
  {
    id: "sample-jason-kaspar",
    nameSlug: "jason-kaspar",
    personName: "Jason Kaspar",
    school: "Texas A&M",
    titanClass: 0,
    category: "Market Views",
    headline:
      "Veritas Ark's Kaspar Makes the Case for Hard Assets in Every Allocation",
    snippet:
      "The fund manager argues that persistent inflation and fiscal pressure have changed the math on real assets, and that most institutional books remain structurally underweight.",
    sourceUrl: "https://www.barrons.com/",
    sourceHost: "barrons.com",
    date: "2026-05-21",
    relevance: 0.93,
  },
  {
    id: "sample-matt-ockwood",
    nameSlug: "matt-ockwood",
    personName: "Matt Ockwood",
    school: "Texas A&M",
    titanClass: 0,
    category: "Funding & Deals",
    headline: "Chambers Energy Capital Closes $1.2B Credit Fund",
    snippet:
      "The energy-focused credit manager wrapped its latest vehicle above target, citing strong demand for capital across upstream and midstream borrowers.",
    sourceUrl: "https://www.bloomberg.com/",
    sourceHost: "bloomberg.com",
    date: "2026-05-18",
    relevance: 0.88,
  },
  {
    id: "sample-ty-popplewell",
    nameSlug: "ty-popplewell",
    personName: "Ty Popplewell",
    school: "Texas A&M",
    titanClass: 0,
    category: "Leadership Moves",
    headline: "Kortright Capital Names Popplewell Head of Portfolio Strategy",
    snippet:
      "The promotion follows several years of expanded responsibility across the firm's multi-strategy book and risk allocation process.",
    sourceUrl: "https://www.hedgeweek.com/",
    sourceHost: "hedgeweek.com",
    date: "2026-05-09",
    relevance: 0.86,
  },
  {
    id: "sample-andrew-robertson",
    nameSlug: "andrew-robertson",
    personName: "Andrew Robertson",
    school: "Texas A&M",
    titanClass: 0,
    category: "Funding & Deals",
    headline: "Robertson Energy & Capital Backs West Texas Midstream Buildout",
    snippet:
      "The deal extends the firm's footprint in Permian gathering and processing, with capital earmarked for new compression and takeaway capacity.",
    sourceUrl: "https://www.reuters.com/",
    sourceHost: "reuters.com",
    date: "2026-05-12",
    relevance: 0.81,
  },
  {
    id: "sample-jon-boben",
    nameSlug: "jon-boben",
    personName: "Jon Boben",
    school: "Texas A&M",
    titanClass: 1,
    category: "Recognition",
    headline: "Akin Gump's Boben Recognized in Chambers USA Energy Rankings",
    snippet:
      "Clients cited his work structuring complex energy financings and project-level transactions across the firm's Texas practice.",
    sourceUrl: "https://www.law360.com/",
    sourceHost: "law360.com",
    date: "2026-04-30",
    relevance: 0.79,
  },
  {
    id: "sample-andy-cronin",
    nameSlug: "andy-cronin",
    personName: "Andy Cronin",
    school: "Texas A&M",
    titanClass: 1,
    category: "Market Views",
    headline: "Lenox Park's Cronin on Closing the Data Gap in Allocations",
    snippet:
      "He makes the case that better manager-level data is reshaping how large allocators evaluate and benchmark their portfolios.",
    sourceUrl: "https://www.institutionalinvestor.com/",
    sourceHost: "institutionalinvestor.com",
    date: "2026-04-15",
    relevance: 0.77,
  },
  {
    id: "sample-will-carpenter",
    nameSlug: "will-carpenter",
    personName: "Will Carpenter",
    school: "Texas A&M",
    titanClass: 0,
    category: "Company News",
    headline: "Texas Teacher Retirement System Expands Private Credit Mandate",
    snippet:
      "The pension's investment team signaled a larger allocation to direct lending as it continues to diversify away from traditional fixed income.",
    sourceUrl: "https://www.pionline.com/",
    sourceHost: "pionline.com",
    date: "2026-04-22",
    relevance: 0.74,
  },
  {
    id: "sample-cason-beckham",
    nameSlug: "cason-beckham",
    personName: "Cason Beckham",
    school: "Texas A&M",
    titanClass: 0,
    category: "Recognition",
    headline: "TRS Texas Investment Team Honored for Returns Performance",
    snippet:
      "The award recognized the public pension's risk-adjusted results over a multi-year horizon against a peer set of large U.S. funds.",
    sourceUrl: "https://www.ai-cio.com/",
    sourceHost: "ai-cio.com",
    date: "2026-04-03",
    relevance: 0.71,
  },
];
