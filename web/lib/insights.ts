import {
  firstEmployerFirms,
  firstEmployerSectors,
  firstJobSectorMembers,
  landingSectorMembers,
  firmClusters,
  currentGeoSpread,
  schoolBreakdown,
  latestInsightsSnapshot,
  SECTOR_CATCHALL,
  type FirmBreakdown,
  type FirmCluster,
  type GeoSpread,
  type SchoolBreakdown,
  type SectorBreakdown,
  type SectorMember,
} from "./db";

// "Overview & Insights" intelligence layer, built to drive two comparable
// views — TRAJECTORY (where Titans start → where they land → how senior they
// get) and SCORECARD (the four headline KPIs). The dataset's unique value is the
// start→now arc: `initial_company` is populated for every alum (where they
// START), while enrichment claims (current_employer, current_title) reveal where
// they LAND and how far they climb.
//
// ALWAYS MEASURED — computed live from the fully-populated `people` roster:
//   schoolSpread (we genuinely know each alum's school).
// MEASURED WHEN ENRICHED — empty (no mock) until the pipeline has data:
//   startFirms + measuredSectors come from the VERIFIED first_employer the
//   pipeline resolves (NOT the roster's program-era initial_company, which we
//   don't trust as a real first post-grad employer); geoSpread prefers the
//   enriched current location; narrative / landingFirms / seniority /
//   currentTitles / signatureStats read the insights_snapshot.
//
// `hasOutcomeData` is false until the snapshot reports at least one enriched
// person; the view uses it (plus per-list emptiness) to switch between empty
// states and real numbers.

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
  // Per-person rows behind each sector card, for the drill-down modal.
  firstJobMembers: SectorMember[];
  landingMembers: SectorMember[];
  total: number;
  // MEASURED WHEN ENRICHED — empty until the pipeline writes a snapshot with
  // at least one enriched person. No mock fallback.
  narrative: string;
  landingFirms: FirmCount[];
  landingSectors: SectorBreakdown[];
  seniority: SeniorityTier[];
  currentTitles: CurrentTitle[];
  signatureStats: SignatureStat[];
  clusters: FirmCluster[];
  // True once a real snapshot reports enriched alumni; drives empty states.
  hasOutcomeData: boolean;
}

// Keep the catch-all in the chart for honesty, but always last so the named
// sectors read as the concentration story. SECTOR_CATCHALL is imported from db
// so the label stays in one place.

export function getAlumniInsights(): AlumniInsights {
  const schoolSpread = schoolBreakdown();
  const total = schoolSpread.reduce((sum, s) => sum + s.count, 0);

  // Push the catch-all to the end so the named sectors read as the concentration
  // story (a big "Other" bar leading the chart buries the real signal).
  const catchAllLast = (rows: SectorBreakdown[]): SectorBreakdown[] => [
    ...rows.filter((s) => s.sector !== SECTOR_CATCHALL),
    ...rows.filter((s) => s.sector === SECTOR_CATCHALL),
  ];

  // First-job sectors from the VERIFIED first employer (empty until enriched).
  const measuredSectors = catchAllLast(firstEmployerSectors());

  // Outcome data is real the moment the pipeline has enriched anyone — we do NOT
  // wait for the is_sample coverage gate, so small test batches are visible. We
  // simply never invent numbers: no snapshot, or zero enriched, → empty states.
  const snapshot = latestInsightsSnapshot();
  const hasOutcomeData = !!snapshot && snapshot.enriched_count > 0;

  return {
    startFirms: firstEmployerFirms(8),
    geoSpread: currentGeoSpread(8),
    schoolSpread,
    measuredSectors,
    firstJobMembers: firstJobSectorMembers(),
    landingMembers: landingSectorMembers(),
    total,
    narrative: hasOutcomeData ? snapshot!.narrative : "",
    landingFirms: hasOutcomeData ? snapshot!.landing_firms : [],
    landingSectors: hasOutcomeData ? catchAllLast(snapshot!.landing_sectors) : [],
    seniority: hasOutcomeData ? snapshot!.seniority : [],
    currentTitles: hasOutcomeData ? snapshot!.current_titles : [],
    signatureStats: hasOutcomeData ? snapshot!.signature_stats : [],
    clusters: firmClusters(8),
    hasOutcomeData,
  };
}
