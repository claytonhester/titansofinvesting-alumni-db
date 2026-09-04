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
// stats, filter options, the insights roll-up, and the news feed. The DB is a
// read-only snapshot fixed for the life of the deploy, so these aggregates are
// computed once per process and reused by every request. The page itself
// stays force-dynamic (it mints a per-request chat token) — this just keeps
// that dynamism from re-running a dozen full-table scans per hit.
export interface HomeAggregates {
  stats: DirectoryStats;
  schools: string[];
  classes: ClassOption[];
  enriched: EnrichedPerson[];
  insights: AlumniInsights;
  newsFeed: NewsFeedData;
  newsTotal: number;
}

let _aggregates: HomeAggregates | null = null;

export function homeAggregates(): HomeAggregates {
  if (_aggregates) return _aggregates;
  _aggregates = {
    stats: directoryStats(),
    schools: listSchools(),
    classes: listClasses(),
    enriched: recentlyEnriched(6),
    insights: getAlumniInsights(),
    newsFeed: getNewsFeed(40),
    newsTotal: curatedNewsCount(),
  };
  return _aggregates;
}
