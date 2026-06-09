import {
  topFirms,
  currentGeoSpread,
  schoolBreakdown,
  sectorBreakdown,
  latestInsightsSnapshot,
  type FirmBreakdown,
  type GeoSpread,
  type SchoolBreakdown,
  type SectorBreakdown,
} from "./db";

// "Overview & Insights" intelligence layer, built to drive two comparable
// views — TRAJECTORY (where Titans start → where they land → how senior they
// get) and SCORECARD (the four headline KPIs). The dataset's unique value is the
// start→now arc: `initial_company` is populated for every alum (where they
// START), while enrichment claims (current_employer, current_title) reveal where
// they LAND and how far they climb.
//
// ALWAYS MEASURED — computed live from the fully-populated `people` roster:
//   startFirms (initial_company), geoSpread, schoolSpread, measuredSectors.
// MEASURED WHEN ENRICHED — read from the pipeline's insights_snapshot once any
// alumnus has been enriched; EMPTY (no mock data) until then so the UI renders
// honest empty states rather than seeded numbers:
//   narrative, landingFirms, seniority ladder, currentTitles, signatureStats.
//
// `hasOutcomeData` is false until the snapshot reports at least one enriched
// person; the view uses it to switch between empty states and real numbers.

export interface FirmCount {
  company: string;
  count: number;
}

export interface SeniorityTier {
  tier: string;
  count: number;
}

export interface CurrentTitle {
  title: string;
  count: number;
}

export interface SignatureStat {
  label: string;
  value: string;
  detail: string;
  pct: number;
}

export interface AlumniInsights {
  // ALWAYS MEASURED — computed live from the people roster.
  startFirms: FirmBreakdown[];
  geoSpread: GeoSpread[];
  schoolSpread: SchoolBreakdown[];
  measuredSectors: SectorBreakdown[];
  total: number;
  // MEASURED WHEN ENRICHED — empty until the pipeline writes a snapshot with
  // at least one enriched person. No mock fallback.
  narrative: string;
  landingFirms: FirmCount[];
  seniority: SeniorityTier[];
  currentTitles: CurrentTitle[];
  signatureStats: SignatureStat[];
  // True once a real snapshot reports enriched alumni; drives empty states.
  hasOutcomeData: boolean;
}

// Keep the catch-all in the chart for honesty, but always last so the named
// sectors read as the concentration story.
const SECTOR_CATCHALL = "Other / Operating";

export function getAlumniInsights(): AlumniInsights {
  const schoolSpread = schoolBreakdown();
  const total = schoolSpread.reduce((sum, s) => sum + s.count, 0);

  const allSectors = sectorBreakdown();
  const named = allSectors.filter((s) => s.sector !== SECTOR_CATCHALL);
  const catchAll = allSectors.filter((s) => s.sector === SECTOR_CATCHALL);
  const measuredSectors = [...named, ...catchAll];

  // Outcome data is real the moment the pipeline has enriched anyone — we do NOT
  // wait for the is_sample coverage gate, so small test batches are visible. We
  // simply never invent numbers: no snapshot, or zero enriched, → empty states.
  const snapshot = latestInsightsSnapshot();
  const hasOutcomeData = !!snapshot && snapshot.enriched_count > 0;

  return {
    startFirms: topFirms(8),
    geoSpread: currentGeoSpread(8),
    schoolSpread,
    measuredSectors,
    total,
    narrative: hasOutcomeData ? snapshot!.narrative : "",
    landingFirms: hasOutcomeData ? snapshot!.landing_firms : [],
    seniority: hasOutcomeData ? snapshot!.seniority : [],
    currentTitles: hasOutcomeData ? snapshot!.current_titles : [],
    signatureStats: hasOutcomeData ? snapshot!.signature_stats : [],
    hasOutcomeData,
  };
}
