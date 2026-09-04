import {
  directoryStats,
  listClasses,
  listSchools,
  recentlyEnriched,
  curatedNewsCount,
  type ClassOption,
  type DirectoryStats,
  type EnrichedPerson,
} from "./db";
import { getNewsFeed } from "./news";
import { getAlumniInsights, type AlumniInsights } from "./insights";
import type { NewsFeedData } from "./news-types";

// Everything on the home page that does NOT depend on the request: directory
// stats, filter options, the insights roll-up, and the news feed.
//
// These were memoised at module scope once (the DB is a read-only snapshot,
// fixed for the life of a deploy). That crashed production: holding the query
// results across requests kept better-sqlite3 objects alive past the end of
// the invocation, and on Vercel's serverless runtime the native addon aborted
// during environment teardown (SIGABRT in RemoveEnvironmentCleanupHook),
// killing ~half of all home-page renders. The queries are a few milliseconds
// against a small local file, so they run per request. Do NOT reintroduce a
// process-lifetime cache of anything that comes out of better-sqlite3.
export interface HomeAggregates {
  stats: DirectoryStats;
  schools: string[];
  classes: ClassOption[];
  enriched: EnrichedPerson[];
  insights: AlumniInsights;
  newsFeed: NewsFeedData;
  newsTotal: number;
}

export function homeAggregates(): HomeAggregates {
  return {
    stats: directoryStats(),
    schools: listSchools(),
    classes: listClasses(),
    enriched: recentlyEnriched(6),
    insights: getAlumniInsights(),
    newsFeed: getNewsFeed(40),
    newsTotal: curatedNewsCount(),
  };
}
