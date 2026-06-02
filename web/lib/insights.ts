import {
  topFirms,
  geoSpread,
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
// get) and SCORECARD (a bento of headline signals). The dataset's unique value
// is the start→now arc: `initial_company` is populated for every alum (where
// they START), while enrichment claims (current_employer, current_title) reveal
// where they LAND and how far they climb.
//
// REAL half — computed live from the fully-populated `people` table:
//   startFirms (initial_company), geoSpread, schoolSpread, heroStats.
// ILLUSTRATIVE half — seeded from the planned enrichment corpus, flagged by
// isSample until a pipeline insights pass produces real rows:
//   landingFirms (current_employer), seniority ladder, sectors, signatureStats.
//
// Swap-ready: when real enrichment rows exist, replace the SAMPLE_INSIGHTS
// block with reads of those rows and flip isSample — no caller changes.

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
  // REAL — computed live from the people table.
  startFirms: FirmBreakdown[];
  geoSpread: GeoSpread[];
  schoolSpread: SchoolBreakdown[];
  measuredSectors: SectorBreakdown[];
  total: number;
  // ILLUSTRATIVE — seeded synthesis, flagged by isSample.
  narrative: string;
  landingFirms: FirmCount[];
  seniority: SeniorityTier[];
  currentTitles: CurrentTitle[];
  signatureStats: SignatureStat[];
  isSample: boolean;
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

  // The illustrative half flips to measured data the moment a real (is_sample=0)
  // snapshot exists — see latestInsightsSnapshot(). Until then we keep the seeded
  // SAMPLE_INSIGHTS so the demo never publishes sparse, half-enriched numbers.
  const snapshot = latestInsightsSnapshot();
  const real = snapshot && !snapshot.is_sample ? snapshot : null;

  return {
    startFirms: topFirms(8),
    geoSpread: geoSpread(8),
    schoolSpread,
    measuredSectors,
    total,
    narrative: real ? real.narrative : SAMPLE_INSIGHTS.narrative,
    landingFirms: real ? real.landing_firms : SAMPLE_INSIGHTS.landingFirms,
    seniority: real ? real.seniority : SAMPLE_INSIGHTS.seniority,
    currentTitles: real ? real.current_titles : SAMPLE_INSIGHTS.currentTitles,
    signatureStats: real ? real.signature_stats : SAMPLE_INSIGHTS.signatureStats,
    isSample: !real,
  };
}

// Seeded to be internally consistent with the real corpus (1,056 alumni; top
// first employers JP Morgan / Bain / PwC; Texas-anchored geography). These
// values illustrate what the enrichment pass will measure for real.
const SAMPLE_INSIGHTS: {
  narrative: string;
  landingFirms: FirmCount[];
  seniority: SeniorityTier[];
  currentTitles: CurrentTitle[];
  signatureStats: SignatureStat[];
} = {
  narrative:
    "The Titan career follows a clear arc. Most start where the training is hardest — the bulge-bracket banks and brand-name consultancies, JP Morgan, Bain, the Big Four — then convert that pedigree into a move buy-side. From there the cohort compounds: a majority now sit at director level or above, and roughly one in five run their own fund or hold a founder's seat. The gravity is unmistakable — investment management, private capital, and the Texas energy economy that anchors the program.",
  landingFirms: [
    { company: "Goldman Sachs", count: 19 },
    { company: "Blackstone", count: 16 },
    { company: "Citadel", count: 13 },
    { company: "Own fund / partnership", count: 12 },
    { company: "Apollo Global", count: 11 },
    { company: "EnCap Investments", count: 10 },
    { company: "Quantum Capital", count: 9 },
    { company: "Vista Equity Partners", count: 8 },
  ],
  seniority: [
    { tier: "Analyst / Associate", count: 142 },
    { tier: "VP / Principal", count: 318 },
    { tier: "Director / Managing Director", count: 271 },
    { tier: "Partner / Founder", count: 224 },
    { tier: "C-suite / Owner", count: 101 },
  ],
  currentTitles: [
    { title: "Managing Director", count: 138 },
    { title: "Partner", count: 96 },
    { title: "Vice President", count: 89 },
    { title: "Portfolio Manager", count: 71 },
    { title: "Principal", count: 64 },
    { title: "Founder / CEO", count: 58 },
    { title: "Director", count: 52 },
    { title: "Associate", count: 41 },
  ],
  signatureStats: [
    {
      label: "Now on the buy-side",
      value: "61%",
      detail: "moved from a bank or consultancy into investing roles",
      pct: 61,
    },
    {
      label: "Reached MD or above",
      value: "57%",
      detail: "director, managing director, partner, or C-suite",
      pct: 57,
    },
    {
      label: "Founders & partners",
      value: "224",
      detail: "running their own fund or holding a partner seat",
      pct: 21,
    },
    {
      label: "Still at their first firm",
      value: "9%",
      detail: "stayed and climbed where they started",
      pct: 9,
    },
  ],
};
