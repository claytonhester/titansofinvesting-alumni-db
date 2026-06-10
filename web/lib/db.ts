import Database from "better-sqlite3";
import fs from "node:fs";
import path from "node:path";

// The pipeline owns all writes. The web app opens the SAME SQLite file
// strictly READ-ONLY — it must never mutate the research database.
//
// Path resolution (in priority order) so the app runs both locally and on a
// serverless host where the repo's pipeline/ dir is NOT in the deployment:
//   1. TITANS_DB_PATH env override (explicit wins).
//   2. ./data/titans.db — the snapshot bundled INTO web/ at build time
//      (see scripts/sync-db.mjs + next.config outputFileTracingIncludes).
//      This is the only copy that ships to Vercel.
//   3. ../pipeline/data/titans.db — local dev fallback when the bundle step
//      hasn't run yet, reading the pipeline's live working copy directly.
function resolveDbPath(): string {
  const override = process.env.TITANS_DB_PATH;
  if (override) return override;
  const bundled = path.join(process.cwd(), "data", "titans.db");
  if (fs.existsSync(bundled)) return bundled;
  return path.join(process.cwd(), "..", "pipeline", "data", "titans.db");
}

let _db: Database.Database | null = null;

function db(): Database.Database {
  if (_db) return _db;
  _db = new Database(resolveDbPath(), { readonly: true, fileMustExist: true });
  _db.pragma("query_only = true");
  _db.pragma("busy_timeout = 5000");
  return _db;
}

export interface Person {
  id: number;
  full_name: string;
  name_slug: string;
  titan_class: number;
  school: string;
  initial_company: string;
  city: string;
  source_url: string;
  needs_review: number;
}

export interface DirectoryFilters {
  q?: string;
  school?: string;
  titanClass?: number;
}

const COLUMNS =
  "id, full_name, name_slug, titan_class, school, initial_company, city, source_url, needs_review";

export function listPeople(filters: DirectoryFilters): Person[] {
  const where: string[] = [];
  const params: Record<string, unknown> = {};

  if (filters.q) {
    where.push("(full_name LIKE :q OR initial_company LIKE :q OR city LIKE :q)");
    params.q = `%${filters.q}%`;
  }
  if (filters.school) {
    where.push("school = :school");
    params.school = filters.school;
  }
  if (filters.titanClass !== undefined) {
    where.push("titan_class = :titanClass");
    params.titanClass = filters.titanClass;
  }

  const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const sql = `SELECT ${COLUMNS} FROM people ${clause}
    ORDER BY school, titan_class, full_name`;
  return db().prepare(sql).all(params) as Person[];
}

export function getPersonBySlug(slug: string): Person | undefined {
  return db()
    .prepare(`SELECT ${COLUMNS} FROM people WHERE name_slug = ? ORDER BY titan_class LIMIT 1`)
    .get(slug) as Person | undefined;
}

export function listSchools(): string[] {
  const rows = db()
    .prepare("SELECT DISTINCT school FROM people ORDER BY school")
    .all() as { school: string }[];
  return rows.map((r) => r.school);
}

export interface ClassOption {
  school: string;
  titan_class: number;
  count: number;
}

export function listClasses(): ClassOption[] {
  return db()
    .prepare(
      `SELECT school, titan_class, COUNT(*) AS count
       FROM people GROUP BY school, titan_class
       ORDER BY school, titan_class`
    )
    .all() as ClassOption[];
}

export interface DirectoryStats {
  total: number;
  schools: number;
  classes: number;
  cities: number;
  enriched: number;
  claims: number;
  sources: number;
}

export function directoryStats(): DirectoryStats {
  const base = db()
    .prepare(
      `SELECT COUNT(*) AS total,
              COUNT(DISTINCT school) AS schools,
              COUNT(DISTINCT school || '|' || titan_class) AS classes,
              COUNT(DISTINCT CASE WHEN city <> '(unknown)' THEN city END) AS cities
       FROM people`
    )
    .get() as Omit<DirectoryStats, "enriched" | "claims" | "sources">;
  const enr = db()
    .prepare(
      `SELECT COUNT(DISTINCT person_id) AS enriched, COUNT(*) AS claims FROM claims`
    )
    .get() as { enriched: number; claims: number };
  const src = db()
    .prepare(`SELECT COUNT(*) AS sources FROM person_sources`)
    .get() as { sources: number };
  return { ...base, ...enr, ...src };
}

export interface SchoolBreakdown {
  school: string;
  count: number;
}

export function schoolBreakdown(): SchoolBreakdown[] {
  return db()
    .prepare(
      `SELECT school, COUNT(*) AS count FROM people
       GROUP BY school ORDER BY count DESC`
    )
    .all() as SchoolBreakdown[];
}

export interface FirmBreakdown {
  company: string;
  count: number;
}

// The directory's initial_company column is polluted with a handful of
// school-name artifacts from upstream parsing; exclude them so the
// "top firms" view reflects actual employers.
const FIRM_EXCLUDE = ["University of Texas", "Texas A&M", "Baylor University"];

export function topFirms(limit = 10): FirmBreakdown[] {
  const placeholders = FIRM_EXCLUDE.map(() => "?").join(", ");
  return db()
    .prepare(
      `SELECT initial_company AS company, COUNT(*) AS count FROM people
       WHERE initial_company <> '' AND initial_company <> '(unknown)'
         AND initial_company NOT IN (${placeholders})
       GROUP BY initial_company ORDER BY count DESC LIMIT ?`
    )
    .all(...FIRM_EXCLUDE, limit) as FirmBreakdown[];
}

export function distinctEmployers(): number {
  const placeholders = FIRM_EXCLUDE.map(() => "?").join(", ");
  const row = db()
    .prepare(
      `SELECT COUNT(DISTINCT initial_company) AS n FROM people
       WHERE initial_company <> '' AND initial_company <> '(unknown)'
         AND initial_company NOT IN (${placeholders})`
    )
    .get(...FIRM_EXCLUDE) as { n: number };
  return row.n;
}

export interface SectorBreakdown {
  sector: string;
  count: number;
}

// Sector concentration, measured from the fully-populated initial_company
// column (every alum has a first employer). Firms are bucketed by keyword;
// anything unmatched falls into "Other / Operating". Read-only — no pipeline
// pass needed. Ordered longest-keyword-first inside each bucket isn't required
// since the first matching sector wins per firm.
const SECTOR_RULES: { sector: string; keywords: string[] }[] = [
  {
    sector: "Investment Banking",
    keywords: [
      "goldman",
      "morgan stanley",
      "j.p. morgan",
      "jp morgan",
      "jpmorgan",
      "bank of america",
      "merrill",
      "citi",
      "credit suisse",
      "barclays",
      "ubs",
      "deutsche bank",
      "lazard",
      "evercore",
      "moelis",
      "jefferies",
      "houlihan",
      "rbc",
      "wells fargo",
      "raymond james",
      "piper",
      "guggenheim",
      "centerview",
    ],
  },
  {
    sector: "Consulting",
    keywords: [
      "mckinsey",
      "bain",
      "boston consulting",
      "bcg",
      "accenture",
      "oliver wyman",
      "l.e.k",
      "booz",
      "alvarez",
      "ats",
      "consulting",
    ],
  },
  {
    sector: "Accounting & Audit",
    keywords: [
      "pwc",
      "pricewaterhouse",
      "deloitte",
      "ernst",
      "kpmg",
      "grant thornton",
      "bdo",
      "ey",
    ],
  },
  {
    sector: "Private Equity & Credit",
    keywords: [
      "blackstone",
      "kkr",
      "carlyle",
      "apollo",
      "tpg",
      "vista",
      "warburg",
      "ares",
      "bain capital",
      "private equity",
      "capital partners",
      "partners",
      "holdings",
      "equity",
    ],
  },
  {
    sector: "Hedge Funds & Asset Mgmt",
    keywords: [
      "citadel",
      "bridgewater",
      "point72",
      "millennium",
      "fidelity",
      "blackrock",
      "vanguard",
      "pimco",
      "wellington",
      "capital management",
      "asset management",
      "investment management",
      "advisors",
      "capital group",
    ],
  },
  {
    sector: "Energy & Real Assets",
    keywords: [
      "exxon",
      "chevron",
      "conocophillips",
      "phillips 66",
      "halliburton",
      "schlumberger",
      "encap",
      "quantum",
      "kinder morgan",
      "energy",
      "petroleum",
      "oil",
      "gas",
      "resources",
      "midstream",
    ],
  },
];

function classifySector(company: string): string {
  const c = company.toLowerCase();
  for (const rule of SECTOR_RULES) {
    if (rule.keywords.some((k) => c.includes(k))) return rule.sector;
  }
  return "Other / Operating";
}

// Valid sector labels the chat planner may request. Kept in sync with
// SECTOR_RULES so an unknown/garbage sector string from the model is ignored
// rather than silently matching nothing.
export const SECTOR_NAMES: readonly string[] = SECTOR_RULES.map((r) => r.sector);

export interface PeopleSearchParams {
  city?: string;
  school?: string;
  titanClass?: number;
  companyKeyword?: string;
  // One of SECTOR_NAMES. Matches the enriched current_sector facet (precise) and
  // falls back to the bucket's first-employer keyword list for un-enriched people.
  sector?: string;
  // A PDL seniority fragment (e.g. "partner", "vp", "director", "senior").
  seniority?: string;
  limit?: number;
}

const SEARCH_MAX_ROWS = 12;

// A firm keyword matches across BOTH the directory's first employer AND the
// enriched current_employer claim. Matching initial_company alone meant "who
// works at X" really answered "who STARTED at X" — overclaiming current
// employment for people who moved on, and missing people who moved INTO the
// firm/sector. Spanning current_employer makes the answer mean what a visitor
// expects and surfaces the enriched (detail-rich) alumni for firm/sector queries.
function firmKeywordClause(bindKey: string): string {
  return (
    `(initial_company LIKE :${bindKey} OR people.id IN (` +
    `SELECT person_id FROM claims ` +
    `WHERE claim_type = 'current_employer' AND value LIKE :${bindKey}))`
  );
}

// Grounded retrieval for the alumni chat. ALL SQL is owned and parameterized
// here — the model only ever produces typed params, never SQL. Read-only.
export function searchPeople(params: PeopleSearchParams): Person[] {
  const where: string[] = [];
  const bind: Record<string, unknown> = {};

  if (params.city && params.city.trim()) {
    where.push("city LIKE :city");
    bind.city = `%${params.city.trim()}%`;
  }
  if (params.school && params.school.trim()) {
    where.push("school LIKE :school");
    bind.school = `%${params.school.trim()}%`;
  }
  if (params.titanClass !== undefined && Number.isFinite(params.titanClass)) {
    where.push("titan_class = :titanClass");
    bind.titanClass = params.titanClass;
  }
  if (params.companyKeyword && params.companyKeyword.trim()) {
    where.push(firmKeywordClause("company"));
    bind.company = `%${params.companyKeyword.trim()}%`;
  }

  // Sector matches the ENRICHED current_sector facet first — a controlled-
  // vocabulary classification of the person's CURRENT employer (so a mover from
  // banking into a hedge fund matches "Hedge Funds & Asset Mgmt" precisely, which
  // an employer-name keyword never would). For un-enriched people (no insights
  // row) we OR in the bucket's first-employer keyword list as a fallback. Only a
  // recognized sector name is honored.
  if (params.sector && SECTOR_NAMES.includes(params.sector)) {
    const ors: string[] = [
      "people.id IN (SELECT person_id FROM person_insights WHERE current_sector = :sectorExact)",
    ];
    bind.sectorExact = params.sector;
    const rule = SECTOR_RULES.find((r) => r.sector === params.sector);
    if (rule) {
      rule.keywords.forEach((kw, i) => {
        const key = `sec${i}`;
        ors.push(firmKeywordClause(key));
        bind[key] = `%${kw}%`;
      });
    }
    where.push(`(${ors.join(" OR ")})`);
  }

  // Seniority matches the enriched pdl_seniority facet (e.g. "partner", "vp",
  // "director", "senior") — only enriched people carry it, so this narrows to
  // people we can actually vouch for at that level.
  if (params.seniority && params.seniority.trim()) {
    where.push(
      "people.id IN (SELECT person_id FROM person_insights WHERE pdl_seniority LIKE :seniority)"
    );
    bind.seniority = `%${params.seniority.trim().toLowerCase()}%`;
  }

  const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const rawLimit = params.limit ?? SEARCH_MAX_ROWS;
  const limit = Math.max(1, Math.min(SEARCH_MAX_ROWS, Math.floor(rawLimit)));

  // Prefer enriched alumni (those with claims) so answers can cite detail,
  // then by school/class for stable ordering.
  const sql = `SELECT ${COLUMNS},
      (SELECT COUNT(*) FROM claims c WHERE c.person_id = people.id) AS claim_count
    FROM people
    ${clause}
    ORDER BY claim_count DESC, school, titan_class, full_name
    LIMIT :limit`;
  bind.limit = limit;
  return db().prepare(sql).all(bind) as Person[];
}

export interface SlugClaim {
  name_slug: string;
  claim_type: string;
  value: string;
  source_url: string;
  quote: string;
  confidence: number;
}

// Attach enriched, source-attributed claims to a set of result slugs so the
// synthesis step can ground specific career facts. Read-only, parameterized.
export function claimsForSlugs(slugs: string[]): SlugClaim[] {
  const clean = slugs.filter((s) => typeof s === "string" && s.length > 0);
  if (clean.length === 0) return [];
  const placeholders = clean.map(() => "?").join(", ");
  return db()
    .prepare(
      `SELECT p.name_slug, c.claim_type, c.value, c.source_url, c.quote, c.confidence
       FROM claims c JOIN people p ON p.id = c.person_id
       WHERE p.name_slug IN (${placeholders})
         AND c.claim_type <> 'news_mention'
       ORDER BY p.name_slug, c.confidence DESC`
    )
    .all(...clean) as SlugClaim[];
}

export function sectorBreakdown(): SectorBreakdown[] {
  const placeholders = FIRM_EXCLUDE.map(() => "?").join(", ");
  const rows = db()
    .prepare(
      `SELECT initial_company AS company, COUNT(*) AS count FROM people
       WHERE initial_company <> '' AND initial_company <> '(unknown)'
         AND initial_company NOT IN (${placeholders})
       GROUP BY initial_company`
    )
    .all(...FIRM_EXCLUDE) as FirmBreakdown[];

  const tally = new Map<string, number>();
  for (const { company, count } of rows) {
    const sector = classifySector(company);
    tally.set(sector, (tally.get(sector) ?? 0) + count);
  }
  return [...tally.entries()]
    .map(([sector, count]) => ({ sector, count }))
    .sort((a, b) => b.count - a.count);
}

// VERIFIED first-employer views. The roster's `initial_company` is the
// program-era listing, not a confirmed first post-grad employer (and is often
// not a first job at all — current ventures, "Texas A&M" for students). So
// "Where they start" reads the first_employer the pipeline resolves during
// enrichment instead. Empty until people are classified; the person_insights
// table may not exist yet, so both are guarded and return [] rather than throw.
export function firstEmployerFirms(limit = 8): FirmBreakdown[] {
  try {
    return db()
      .prepare(
        `SELECT first_employer AS company, COUNT(*) AS count
         FROM person_insights
         WHERE TRIM(first_employer) <> ''
         GROUP BY first_employer ORDER BY count DESC LIMIT ?`
      )
      .all(limit) as FirmBreakdown[];
  } catch {
    return [];
  }
}

export function firstEmployerSectors(): SectorBreakdown[] {
  let rows: FirmBreakdown[];
  try {
    rows = db()
      .prepare(
        `SELECT first_employer AS company, COUNT(*) AS count
         FROM person_insights
         WHERE TRIM(first_employer) <> ''
         GROUP BY first_employer`
      )
      .all() as FirmBreakdown[];
  } catch {
    return [];
  }
  const tally = new Map<string, number>();
  for (const { company, count } of rows) {
    const sector = classifySector(company);
    tally.set(sector, (tally.get(sector) ?? 0) + count);
  }
  return [...tally.entries()]
    .map(([sector, count]) => ({ sector, count }))
    .sort((a, b) => b.count - a.count);
}

export interface ClassSpread {
  titan_class: number;
  count: number;
}

export function classSpread(): ClassSpread[] {
  return db()
    .prepare(
      `SELECT titan_class, COUNT(*) AS count FROM people
       GROUP BY titan_class ORDER BY titan_class`
    )
    .all() as ClassSpread[];
}

export interface GeoSpread {
  city: string;
  count: number;
}

export function geoSpread(limit = 8): GeoSpread[] {
  return db()
    .prepare(
      `SELECT city, COUNT(*) AS count FROM people
       WHERE city <> '' AND city <> '(unknown)'
       GROUP BY city ORDER BY count DESC LIMIT ?`
    )
    .all(limit) as GeoSpread[];
}

// Map view: each person's CURRENT location, from the enriched `location` claim
// only. We deliberately do NOT fall back to the program-era roster city — that's
// where they *were*, not where they *are*, and showing it would assert a current
// location we haven't verified. So this is empty until enrichment runs, and the
// Map renders an empty state. City buckets are normalized to the segment before
// the first comma so "Austin, TX" and "Austin" tally together.
export function currentGeoSpread(limit = 8): GeoSpread[] {
  const rows = db()
    .prepare(
      `SELECT TRIM(value) AS raw
       FROM claims
       WHERE claim_type = 'location' AND TRIM(value) <> ''`
    )
    .all() as { raw: string }[];

  const tally = new Map<string, number>();
  for (const { raw } of rows) {
    const city = (raw ?? "").split(",")[0].trim();
    if (!city || city === "(unknown)") continue;
    tally.set(city, (tally.get(city) ?? 0) + 1);
  }
  return [...tally.entries()]
    .map(([city, count]) => ({ city, count }))
    .sort((a, b) => b.count - a.count)
    .slice(0, limit);
}

export interface EnrichedPerson {
  name_slug: string;
  full_name: string;
  initial_company: string;
  school: string;
  titan_class: number;
  claim_count: number;
}

export function recentlyEnriched(limit = 6): EnrichedPerson[] {
  return db()
    .prepare(
      `SELECT p.name_slug, p.full_name, p.initial_company, p.school,
              p.titan_class, COUNT(c.id) AS claim_count
       FROM people p JOIN claims c ON c.person_id = p.id
       GROUP BY p.id ORDER BY claim_count DESC LIMIT ?`
    )
    .all(limit) as EnrichedPerson[];
}

export interface NewsMention {
  name_slug: string;
  full_name: string;
  school: string;
  titan_class: number;
  value: string;
  source_url: string;
  quote: string;
}

// news_mention claims are name-matched (GNews), NOT identity-verified —
// surfaced in a clearly-labeled "In the news" view, never the verified résumé.
export function recentNews(limit = 40): NewsMention[] {
  return db()
    .prepare(
      `SELECT p.name_slug, p.full_name, p.school, p.titan_class,
              c.value, c.source_url, c.quote
       FROM claims c JOIN people p ON p.id = c.person_id
       WHERE c.claim_type = 'news_mention'
       ORDER BY c.value DESC LIMIT ?`
    )
    .all(limit) as NewsMention[];
}

export function newsCount(): number {
  const row = db()
    .prepare(`SELECT COUNT(*) AS n FROM claims WHERE claim_type = 'news_mention'`)
    .get() as { n: number };
  return row.n;
}

// The CURATED news feed: the Haiku news agent's category + summary + importance
// per article, joined back to the person. This is what the "In the news" tab
// reads — never the raw, uncategorized news_mention claims. Guarded: returns []
// when the news_curated table doesn't exist yet (pre-first-run).
export interface CuratedNewsRow {
  name_slug: string;
  full_name: string;
  school: string;
  titan_class: number;
  headline: string;
  summary: string;
  category: string;
  date: string;
  source_url: string;
  source_host: string;
  importance: number;
}

export function curatedNews(limit = 40): CuratedNewsRow[] {
  try {
    return db()
      .prepare(
        `SELECT p.name_slug, p.full_name, p.school, p.titan_class,
                n.headline, n.summary, n.category, n.date,
                n.source_url, n.source_host, n.importance
         FROM news_curated n JOIN people p ON p.id = n.person_id
         ORDER BY n.importance DESC, n.date DESC LIMIT ?`
      )
      .all(limit) as CuratedNewsRow[];
  } catch {
    return [];
  }
}

// One person's curated feed — the same editorial gate the homepage tab uses,
// scoped to a person for their profile page. Never the raw news_mention claims,
// so a low-signal mention (a bio page, a passing name-drop) the curator dropped
// does not resurface here. Ordered best-first.
export function curatedNewsForPerson(personId: number): CuratedNewsRow[] {
  try {
    return db()
      .prepare(
        `SELECT p.name_slug, p.full_name, p.school, p.titan_class,
                n.headline, n.summary, n.category, n.date,
                n.source_url, n.source_host, n.importance
         FROM news_curated n JOIN people p ON p.id = n.person_id
         WHERE n.person_id = ?
         ORDER BY n.importance DESC, n.date DESC`
      )
      .all(personId) as CuratedNewsRow[];
  } catch {
    return [];
  }
}

export function curatedNewsCount(): number {
  try {
    const row = db()
      .prepare(`SELECT COUNT(*) AS n FROM news_curated`)
      .get() as { n: number };
    return row.n;
  } catch {
    return 0;
  }
}

export interface Claim {
  claim_type: string;
  value: string;
  source_url: string;
  quote: string;
  confidence: number;
  extraction_method: string;
}

export function getClaimsForPerson(personId: number): Claim[] {
  return db()
    .prepare(
      `SELECT claim_type, value, source_url, quote, confidence, extraction_method
       FROM claims WHERE person_id = ?
       ORDER BY claim_type, confidence DESC`
    )
    .all(personId) as Claim[];
}

// The Phase-3 aggregate roll-up (one row per year, written by the pipeline's
// phase3_insights pass). Mirrors pipeline/insights_store.InsightsSnapshot. The
// scalar columns plus the deserialized JSON payload drive the real half of the
// "Overview & Insights" view once enrichment coverage is high enough that the
// pipeline flips is_sample to 0.
export interface InsightsSnapshot {
  snapshot_year: number;
  people_total: number;
  enriched_count: number;
  coverage: number;
  is_sample: boolean;
  narrative: string;
  landing_firms: { company: string; count: number }[];
  current_titles: { title: string; count: number }[];
  seniority: { tier: string; count: number }[];
  signature_stats: { label: string; value: string; detail: string; pct: number }[];
  landing_sectors: { sector: string; count: number }[];
  founders_partners: number;
}

interface SnapshotRow {
  snapshot_year: number;
  people_total: number;
  enriched_count: number;
  coverage: number;
  is_sample: number;
  narrative: string;
  payload: string;
}

// The insights_snapshot table does not exist until the pipeline's phase3 pass
// has run at least once. Until then this returns null and the web keeps its
// seeded illustration — never throws on the missing table.
export function latestInsightsSnapshot(): InsightsSnapshot | null {
  let row: SnapshotRow | undefined;
  try {
    row = db()
      .prepare(
        "SELECT snapshot_year, people_total, enriched_count, coverage, is_sample, narrative, payload FROM insights_snapshot ORDER BY snapshot_year DESC LIMIT 1"
      )
      .get() as SnapshotRow | undefined;
  } catch {
    return null;
  }
  if (!row) return null;

  const payload = JSON.parse(row.payload) as {
    landing_firms?: { company: string; count: number }[];
    current_titles?: { title: string; count: number }[];
    seniority?: { tier: string; count: number }[];
    signature_stats?: { label: string; value: string; detail: string; pct: number }[];
    landing_sectors?: { sector: string; count: number }[];
    founders_partners?: number;
  };

  return {
    snapshot_year: row.snapshot_year,
    people_total: row.people_total,
    enriched_count: row.enriched_count,
    coverage: row.coverage,
    is_sample: Boolean(row.is_sample),
    narrative: row.narrative,
    landing_firms: payload.landing_firms ?? [],
    current_titles: payload.current_titles ?? [],
    seniority: payload.seniority ?? [],
    signature_stats: payload.signature_stats ?? [],
    landing_sectors: payload.landing_sectors ?? [],
    founders_partners: payload.founders_partners ?? 0,
  };
}

// Same-firm alumni clusters — live, from the enriched current_employer claims
// joined back to people. Powers the "Where Titans cluster" panel and answers the
// app's core "who else can I talk to at X" question. Guarded: returns [] when the
// claims table has no current_employer rows yet. Member names are capped per firm
// for display; `count` is the true total.
export interface FirmCluster {
  company: string;
  count: number;
  members: string[];
}

export function firmClusters(limit = 8, membersPerFirm = 6): FirmCluster[] {
  let rows: { company: string; full_name: string }[];
  try {
    rows = db()
      .prepare(
        `SELECT TRIM(c.value) AS company, p.full_name AS full_name
         FROM claims c JOIN people p ON p.id = c.person_id
         WHERE c.claim_type = 'current_employer' AND TRIM(c.value) <> ''`
      )
      .all() as { company: string; full_name: string }[];
  } catch {
    return [];
  }
  const byFirm = new Map<string, string[]>();
  for (const { company, full_name } of rows) {
    const list = byFirm.get(company) ?? [];
    list.push(full_name);
    byFirm.set(company, list);
  }
  return [...byFirm.entries()]
    .map(([company, members]) => ({
      company,
      count: members.length,
      members: members.slice(0, membersPerFirm),
    }))
    .filter((c) => c.count >= 2) // a "cluster" needs at least two Titans
    .sort((a, b) => b.count - a.count)
    .slice(0, limit);
}

// ---------------------------------------------------------------------------
// Company layer — cached firmographics (pipeline/company_enrich.py), keyed by
// domain, linked to alumni via person_insights.employer_domain. Read-only here.
// ---------------------------------------------------------------------------

export interface Company {
  domain: string;
  slug: string;
  name: string;
  industry: string;
  industryV2: string;
  size: string;
  employeeCount: number | null;
  companyType: string;
  ticker: string;
  founded: number | null;
  hqLocation: string;
  linkedinUrl: string;
  summary: string;
  tags: string[];
}

// Slug from the firm name ("Boston Consulting Group (BCG)" -> "boston-consulting-
// group-bcg"). Domain is the unique key; the slug is for clean URLs. Collisions
// are resolved by getCompanyBySlug falling back to a domain match.
export function companySlug(name: string, domain: string): string {
  const base = (name || domain)
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return base || domain.replace(/\./g, "-");
}

interface CompanyRow {
  domain: string;
  name: string;
  industry: string;
  industry_v2: string;
  size: string;
  employee_count: number | null;
  company_type: string;
  ticker: string;
  founded: number | null;
  hq_location: string;
  linkedin_url: string;
  summary: string;
  tags: string;
  matched: number;
}

function toCompany(r: CompanyRow): Company {
  return {
    domain: r.domain,
    slug: companySlug(r.name, r.domain),
    name: r.name || r.domain,
    industry: r.industry || "",
    industryV2: r.industry_v2 || "",
    size: r.size || "",
    employeeCount: r.employee_count,
    companyType: r.company_type || "",
    ticker: r.ticker || "",
    founded: r.founded,
    hqLocation: r.hq_location || "",
    linkedinUrl: r.linkedin_url || "",
    summary: r.summary || "",
    tags: (r.tags || "").split(",").filter(Boolean),
  };
}

// All MATCHED firms (no-match sentinels excluded). Returns [] before the table
// exists (pre-first-run), so the app degrades cleanly.
function allMatchedCompanies(): CompanyRow[] {
  try {
    return db()
      .prepare(`SELECT * FROM companies WHERE matched = 1`)
      .all() as CompanyRow[];
  } catch {
    return [];
  }
}

// The matched firm for a person, via person_insights.employer_domain. Drives the
// clickable firm chip on the profile. null when unmatched / not enriched.
export function getCompanyForPerson(personId: number): Company | null {
  try {
    const row = db()
      .prepare(
        `SELECT c.* FROM companies c
         JOIN person_insights pi ON pi.employer_domain = c.domain
         WHERE pi.person_id = ? AND c.matched = 1`
      )
      .get(personId) as CompanyRow | undefined;
    return row ? toCompany(row) : null;
  } catch {
    return null;
  }
}

export function getCompanyBySlug(slug: string): Company | null {
  const match = allMatchedCompanies()
    .map(toCompany)
    .find((c) => c.slug === slug || c.domain.replace(/\./g, "-") === slug);
  return match ?? null;
}

export interface TitanLink {
  name_slug: string;
  full_name: string;
  school: string;
  titan_class: number;
  title: string;
  start_year: number | null;
  end_year: number | null;
  is_current: boolean;
}

// Every Titan tied to this firm across their WHOLE career (person_company), with
// role + years, split into who's there now vs who passed through. Powers the
// company page's institutional-memory view. Current first, then most-recent past.
export function titansAtCompany(domain: string): {
  current: TitanLink[];
  past: TitanLink[];
} {
  let rows: (TitanLink & { is_current_int: number })[] = [];
  try {
    rows = db()
      .prepare(
        `SELECT p.name_slug, p.full_name, p.school, p.titan_class,
                pc.title AS title, pc.start_year, pc.end_year,
                pc.is_current AS is_current_int
         FROM person_company pc
         JOIN people p ON p.id = pc.person_id
         WHERE pc.domain = ?
         ORDER BY pc.is_current DESC, pc.end_year DESC, p.full_name`
      )
      .all(domain) as (TitanLink & { is_current_int: number })[];
  } catch {
    return { current: [], past: [] };
  }
  const norm = (r: TitanLink & { is_current_int: number }): TitanLink => ({
    name_slug: r.name_slug,
    full_name: r.full_name,
    school: r.school,
    titan_class: r.titan_class,
    title: r.title || "",
    start_year: r.start_year,
    end_year: r.end_year,
    is_current: Boolean(r.is_current_int),
  });
  return {
    current: rows.filter((r) => r.is_current_int).map(norm),
    past: rows.filter((r) => !r.is_current_int).map(norm),
  };
}

// Top employers by # of Titans, joined to their enriched firm record (for the
// overview leaderboard with clickable company pages).
export interface TopCompany extends Company {
  count: number;
}

export function topCompanies(limit = 12): TopCompany[] {
  try {
    const rows = db()
      .prepare(
        `SELECT c.*, COUNT(pi.person_id) AS count
         FROM companies c
         JOIN person_insights pi ON pi.employer_domain = c.domain
         WHERE c.matched = 1
         GROUP BY c.domain
         ORDER BY count DESC, c.employee_count DESC
         LIMIT ?`
      )
      .all(limit) as (CompanyRow & { count: number })[];
    return rows.map((r) => ({ ...toCompany(r), count: r.count }));
  } catch {
    return [];
  }
}
