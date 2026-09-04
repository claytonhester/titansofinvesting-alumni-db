import type DatabaseNs from "better-sqlite3";
import { createRequire } from "node:module";
import fs from "node:fs";
import path from "node:path";

// better-sqlite3 is a NATIVE addon, and it is loaded on FIRST QUERY rather than
// at module load. A top-level import pulls the .node binary in while the
// serverless instance is still booting, and on Vercel that reliably aborted the
// process on cold starts:
//
//   node[4]: void node::RemoveEnvironmentCleanupHook(...) at hooks.cc:142
//   Node.js process exited with signal: 6 (SIGABRT)
//
// Every warm request succeeded; every burst that forced new instances produced
// a batch of 500s. Requiring the addon lazily, inside the request, keeps it out
// of the boot path. Keep it lazy.
const requireCjs = createRequire(import.meta.url);
type DatabaseInstance = DatabaseNs.Database;

// The pipeline owns all writes. The web app opens the SAME SQLite file
// strictly READ-ONLY — it must never mutate the research database.
//
// Path resolution searches an ordered list of candidates so the app runs both
// locally and on a serverless host where pipeline/ is NOT deployed. The tricky
// part is WHERE the bundled DB lands on Vercel: Next forces the file-tracing
// ROOT to the repo root (/vercel/path0) even though the app lives in web/, so
// the include traced via next.config can land at either `<cwd>/data/titans.db`
// or `<cwd>/web/data/titans.db` depending on which root was in effect. We probe
// both layouts (plus the local pipeline copy) and use the first that exists.
//   1. TITANS_DB_PATH env override (explicit wins).
//   2. <cwd>/data/titans.db          — real DB, bundle when tracing root == web/
//   3. <cwd>/web/data/titans.db      — real DB, bundle when tracing root == repo root
//   4. <cwd>/data/sample.db          — synthetic fallback (open-source clone)
//   5. <cwd>/web/data/sample.db      — synthetic fallback (other tracing root)
//   6. <cwd>/../pipeline/data/...    — local dev fallback (pre-sync)
//
// The titans.db candidates come first so a real deploy uses real data; the
// committed sample.db makes a fresh clone run with fake data out of the box.
function resolveDbPath(): string {
  const override = process.env.TITANS_DB_PATH;
  if (override) return override;

  const candidates = [
    path.join(process.cwd(), "data", "titans.db"),
    path.join(process.cwd(), "web", "data", "titans.db"),
    path.join(process.cwd(), "data", "sample.db"),
    path.join(process.cwd(), "web", "data", "sample.db"),
    path.join(process.cwd(), "..", "pipeline", "data", "titans.db"),
  ];
  for (const candidate of candidates) {
    if (fs.existsSync(candidate)) return candidate;
  }

  // Nothing found — surface cwd + everything we tried so the serverless log
  // shows exactly where the bundle ended up (or didn't) instead of a bare
  // "unable to open database file".
  console.error(
    `[titans] DB not found. cwd=${process.cwd()} tried=${candidates.join(", ")}`,
  );
  return candidates[0];
}

let _db: DatabaseInstance | null = null;

function db(): DatabaseInstance {
  if (_db) return _db;
  const Database = requireCjs("better-sqlite3") as typeof DatabaseNs;
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
  // When true, restrict to people we actually have data on (≥1 verified claim) —
  // the same "enriched" definition the Overview counts. Default behavior is set
  // by the caller (the directory defaults it ON).
  enrichedOnly?: boolean;
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
  if (filters.enrichedOnly) {
    where.push("id IN (SELECT DISTINCT person_id FROM claims)");
  }

  const clause = where.length ? `WHERE ${where.join(" AND ")}` : "";
  const sql = `SELECT ${COLUMNS} FROM people ${clause}
    ORDER BY school, titan_class, full_name`;
  return db().prepare(sql).all(params) as Person[];
}

// A slug can be shared by namesakes in different classes. With no class given
// the earliest class wins, so every existing `/person/<slug>` URL keeps
// resolving; pass `titanClass` (the `?c=` query personHref emits) to reach a
// later-class namesake.
export function getPersonBySlug(
  slug: string,
  titanClass?: number
): Person | undefined {
  const plain = db()
    .prepare(`SELECT ${COLUMNS} FROM people WHERE name_slug = ? ORDER BY titan_class LIMIT 1`)
    .get(slug) as Person | undefined;
  if (titanClass === undefined || !Number.isFinite(titanClass)) return plain;
  // A class only disambiguates a slug shared by namesakes. For a unique slug a
  // stale or hand-edited ?c= must not 404 the one person it can only mean, so
  // the class filter applies only when the slug actually collides.
  if (!collidingSlugs().has(slug)) return plain;
  return db()
    .prepare(
      `SELECT ${COLUMNS} FROM people WHERE name_slug = ? AND titan_class = ? LIMIT 1`
    )
    .get(slug, titanClass) as Person | undefined;
}

let _collidingSlugs: Set<string> | null = null;

// Slugs shared by more than one alum. Memoized per process: the DB is a
// read-only per-deploy snapshot, so the set cannot change underneath us.
function collidingSlugs(): Set<string> {
  if (_collidingSlugs) return _collidingSlugs;
  const rows = db()
    .prepare(`SELECT name_slug FROM people GROUP BY name_slug HAVING COUNT(*) > 1`)
    .all() as { name_slug: string }[];
  _collidingSlugs = new Set(rows.map((r) => r.name_slug));
  return _collidingSlugs;
}

// The canonical profile link for a person. A unique slug keeps the plain
// `/person/<slug>` form; a slug shared by namesakes gets `?c=<titan_class>` so
// every alum is reachable. EVERY profile link the app renders goes through here.
export function personHref(slug: string, titanClass: number): string {
  return collidingSlugs().has(slug)
    ? `/person/${slug}?c=${titanClass}`
    : `/person/${slug}`;
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
  /** Avg 0-100 profile-quality score over enriched people (0 = not computed). */
  completenessAvg: number;
  /** Enriched people scoring below 60 — the refresh-candidate count. */
  completenessLow: number;
  /** Identity sources awaiting a human verdict (decision='review'). null when
   *  the snapshot carries no identity_candidates at all — the public display DB
   *  (pipeline/make_display_db.py) empties that table, and "0 awaiting review"
   *  would then be a false claim rather than a count. */
  reviewQueue: number | null;
}

/** Profile-quality metrics live in pipeline-written tables that may predate
 *  this feature in an older DB snapshot — degrade to zeros, never throw. */
function profileQualityStats(): Pick<
  DirectoryStats,
  "completenessAvg" | "completenessLow" | "reviewQueue"
> {
  let completenessAvg = 0;
  let completenessLow = 0;
  let reviewQueue: number | null = null;
  try {
    const row = db()
      .prepare(
        `SELECT ROUND(AVG(completeness_score)) AS avg,
                SUM(CASE WHEN completeness_score < 60 THEN 1 ELSE 0 END) AS low
         FROM person_insights pi
         WHERE EXISTS (SELECT 1 FROM claims c WHERE c.person_id = pi.person_id)`
      )
      .get() as { avg: number | null; low: number | null };
    completenessAvg = row.avg ?? 0;
    completenessLow = row.low ?? 0;
  } catch {
    /* person_insights or the column missing in this snapshot */
  }
  try {
    const row = db()
      .prepare(
        `SELECT COUNT(*) AS total,
                SUM(CASE WHEN decision = 'review' THEN 1 ELSE 0 END) AS review
         FROM identity_candidates`
      )
      .get() as { total: number; review: number | null };
    reviewQueue = row.total > 0 ? (row.review ?? 0) : null;
  } catch {
    /* identity_candidates missing in this snapshot */
  }
  return { completenessAvg, completenessLow, reviewQueue };
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
    .get() as Omit<
      DirectoryStats,
      "enriched" | "claims" | "sources" | "completenessAvg" | "completenessLow" | "reviewQueue"
    >;
  const enr = db()
    .prepare(
      `SELECT COUNT(DISTINCT person_id) AS enriched, COUNT(*) AS claims FROM claims`
    )
    .get() as { enriched: number; claims: number };
  // Distinct claim sources, not person_sources: the latter is internal research
  // scaffolding that the public display DB ships empty, whereas claims (and
  // their source URLs) are exactly what the site already renders.
  const src = db()
    .prepare(
      `SELECT COUNT(DISTINCT source_url) AS sources FROM claims WHERE source_url <> ''`
    )
    .get() as { sources: number };
  return { ...base, ...enr, ...src, ...profileQualityStats() };
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

export interface SectorBreakdown {
  sector: string;
  count: number;
}

// Sector classification — the TS mirror of pipeline `sector_classify.py`. Keep
// INDUSTRY_MAP, SECTOR_RULES, SECTOR_CATCHALL, and SECTOR_NAMES byte-for-byte in
// sync with that module (pipeline/tests/test_sector_sync.py parses these
// literals out of this file and asserts they match). Two signals, in priority
// order: PDL industry wins when it maps; otherwise employer-name keywords;
// otherwise the catch-all. The web reads the pipeline's STORED sectors for
// display; only SECTOR_RULES is queried live (searchPeople's facet fallback).
// INDUSTRY_MAP / classifySector stay here purely as the sync-guarded mirror.
export const SECTOR_CATCHALL = "Other / Operating";

// PDL `current_industry` -> sector. Checked first; first substring match wins.
// Bare "financial services" / "research" are intentionally unmapped (too
// ambiguous) — they fall through to keywords.
const INDUSTRY_MAP: { sector: string; needles: string[] }[] = [
  { sector: "Real Estate", needles: ["real estate", "commercial real estate", "reit"] },
  { sector: "Law / Legal", needles: ["law practice", "legal services", "legal"] },
  {
    sector: "Technology",
    needles: [
      "computer software",
      "information technology",
      "computer hardware",
      "internet",
      "semiconductor",
      "software",
      "computer networking",
      "information services",
      "consumer electronics",
    ],
  },
  {
    sector: "Healthcare & Life Sciences",
    needles: [
      "hospital",
      "health care",
      "healthcare",
      "medical practice",
      "pharmaceutical",
      "biotechnology",
      "health, wellness",
      "mental health",
      "medical device",
    ],
  },
  { sector: "Insurance", needles: ["insurance"] },
  {
    sector: "Education & Academia",
    needles: ["higher education", "education management", "e-learning", "edtech"],
  },
  {
    sector: "Government & Nonprofit",
    needles: [
      "non-profit",
      "nonprofit",
      "government administration",
      "philanthropy",
      "public policy",
      "think tanks",
      "international affairs",
      "civic",
      "political organization",
    ],
  },
  { sector: "Private Equity & Credit", needles: ["venture capital", "private equity"] },
  { sector: "Hedge Funds & Asset Mgmt", needles: ["investment management", "asset management"] },
  { sector: "Investment Banking", needles: ["investment banking", "banking"] },
  { sector: "Consulting", needles: ["management consulting"] },
  { sector: "Accounting & Audit", needles: ["accounting"] },
  { sector: "Energy & Real Assets", needles: ["oil & energy", "oil", "utilities", "mining", "renewables"] },
];

// Employer-name keywords (fallback when industry is absent or unmapped).
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
      "bain & company",
      "boston consulting",
      "bcg",
      "accenture",
      "oliver wyman",
      "l.e.k",
      "booz",
      "alvarez",
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
      "ey ",
    ],
  },
  {
    sector: "Law / Legal",
    keywords: [
      "law firm",
      "law offices",
      "llp",
      "attorneys",
      "akin gump",
      "kirkland",
      "latham",
      "skadden",
      "sidley",
      "vinson",
      "baker botts",
      "jones day",
      "gibson dunn",
      "wachtell",
      "& feld",
    ],
  },
  {
    sector: "Real Estate",
    keywords: [
      "real estate",
      "realty",
      "properties",
      "property group",
      "cbre",
      "jll",
      "hines",
      "trammell crow",
      "american campus",
      "realtors",
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
    sector: "Healthcare & Life Sciences",
    keywords: [
      "hospital",
      "health system",
      "healthcare",
      "health care",
      "clinic",
      "pharma",
      "biotech",
      "abbott",
      "medtronic",
      "pfizer",
      "merck",
    ],
  },
  {
    sector: "Technology",
    keywords: [
      "google",
      "microsoft",
      "amazon",
      "meta",
      "apple",
      "salesforce",
      "oracle",
      "nvidia",
      "software",
      "technologies",
      "labs",
      "ai",
    ],
  },
  {
    sector: "Insurance",
    keywords: [
      "insurance",
      "assurance",
      "reinsurance",
      "aig",
      "chubb",
      "metlife",
      "prudential",
      "allstate",
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
  {
    sector: "Education & Academia",
    keywords: ["university", "college", "school district", "academy", "institute"],
  },
  {
    sector: "Government & Nonprofit",
    keywords: [
      "foundation",
      "nonprofit",
      "non-profit",
      "department of",
      "city of",
      "county of",
      "federal",
      "ministry",
      "united nations",
    ],
  },
];

function matchIndustry(industry: string): string | null {
  const s = industry.toLowerCase().trim();
  if (!s) return null;
  for (const { sector, needles } of INDUSTRY_MAP) {
    if (needles.some((n) => s.includes(n))) return sector;
  }
  return null;
}

function matchCompany(company: string): string | null {
  const c = company.toLowerCase();
  if (!c.trim()) return null;
  for (const rule of SECTOR_RULES) {
    if (rule.keywords.some((k) => c.includes(k))) return rule.sector;
  }
  return null;
}

// PDL industry wins when it maps; else employer-name keywords; else catch-all.
export function classifySector(company: string, industry = ""): string {
  return matchIndustry(industry) ?? matchCompany(company) ?? SECTOR_CATCHALL;
}

// Every sector label this taxonomy can emit, in display/priority order, catch-
// all last. Mirrors py SECTOR_NAMES. Used to validate chat-planner sector
// requests and to drive the search facet fallback.
export const SECTOR_NAMES: readonly string[] = [
  "Investment Banking",
  "Private Equity & Credit",
  "Hedge Funds & Asset Mgmt",
  "Consulting",
  "Accounting & Audit",
  "Energy & Real Assets",
  "Real Estate",
  "Law / Legal",
  "Technology",
  "Healthcare & Life Sciences",
  "Insurance",
  "Education & Academia",
  "Government & Nonprofit",
  SECTOR_CATCHALL,
];

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

// Fetch specific people by slug (semantic-search hits arrive as ranked slugs).
// Preserves the caller's slug order so semantic ranking survives the round-trip.
export function peopleBySlugs(slugs: string[]): Person[] {
  const clean = slugs.filter((s) => typeof s === "string" && s.length > 0);
  if (clean.length === 0) return [];
  const placeholders = clean.map(() => "?").join(", ");
  const rows = db()
    .prepare(`SELECT ${COLUMNS} FROM people WHERE name_slug IN (${placeholders})`)
    .all(...clean) as Person[];
  const order = new Map(clean.map((s, i) => [s, i]));
  return rows.sort(
    (a, b) => (order.get(a.name_slug) ?? 0) - (order.get(b.name_slug) ?? 0)
  );
}

export interface PersonVector {
  name_slug: string;
  vec: Float32Array;
}

// Load every person's semantic vector (Float32 BLOB) joined to its slug. Returns
// [] when the vectors haven't been built yet (person_vectors absent) so semantic
// retrieval degrades to keyword/facet search rather than throwing.
export function loadPersonVectors(): PersonVector[] {
  const hasTable = db()
    .prepare(
      "SELECT 1 FROM sqlite_master WHERE type='table' AND name='person_vectors'"
    )
    .get();
  if (!hasTable) return [];
  const rows = db()
    .prepare(
      `SELECT p.name_slug AS name_slug, v.vec AS vec
         FROM person_vectors v JOIN people p ON p.id = v.person_id`
    )
    .all() as { name_slug: string; vec: Buffer }[];
  return rows.map((r) => ({
    name_slug: r.name_slug,
    vec: new Float32Array(
      r.vec.buffer,
      r.vec.byteOffset,
      Math.floor(r.vec.byteLength / 4)
    ),
  }));
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
  // Reads the STORED first_sector (classified by the pipeline under the full
  // industry+keyword+Haiku taxonomy), not a live name-keyword guess — so the
  // card matches the modal and the landing card exactly.
  try {
    return db()
      .prepare(
        `SELECT first_sector AS sector, COUNT(*) AS count
         FROM person_insights
         WHERE TRIM(COALESCE(first_sector, '')) <> ''
         GROUP BY first_sector
         ORDER BY count DESC`
      )
      .all() as SectorBreakdown[];
  } catch {
    return [];
  }
}

export interface SectorMember {
  sector: string;
  name: string;
  slug: string;
  school: string;
  titanClass: number;
  employer: string;
  industry: string;
  // Profile link (namesake-safe, see personHref) — client components render
  // this rather than rebuilding `/person/<slug>` themselves.
  href: string;
}

// Attach the canonical profile link to a member row.
function withHref<T extends { slug: string; titanClass: number }>(
  row: T
): T & { href: string } {
  return { ...row, href: personHref(row.slug, row.titanClass) };
}

// Per-person rows behind the LANDING sector card: current employer + raw PDL
// industry, joined to the person. Powers the drill-down modal — every person in
// every bucket, nothing collapsed. Ordered so the modal can group by sector.
export function landingSectorMembers(): SectorMember[] {
  try {
    return db()
      .prepare(
        `SELECT
           pi.current_sector AS sector,
           p.full_name       AS name,
           p.name_slug       AS slug,
           p.school          AS school,
           p.titan_class     AS titanClass,
           COALESCE((SELECT c.value FROM claims c
                       WHERE c.person_id = pi.person_id
                         AND c.claim_type = 'current_employer' LIMIT 1), '') AS employer,
           COALESCE(pi.current_industry, '') AS industry
         FROM person_insights pi
         JOIN people p ON p.id = pi.person_id
         WHERE TRIM(COALESCE(pi.current_sector, '')) <> ''
         ORDER BY pi.current_sector, p.full_name`
      )
      .all()
      .map((r) => withHref(r as Omit<SectorMember, "href">));
  } catch {
    return [];
  }
}

// --- KPI scorecard drill-downs ------------------------------------------

export interface KpiMember {
  name: string;
  slug: string;
  school: string;
  titanClass: number;
  // Profile link (namesake-safe, see personHref).
  href: string;
  // KPI-specific context line (role · firm, "N yrs to MD", current city, …).
  detail: string;
  // Raw numeric for distribution KPIs (years_to_senior_leadership, tenure); null otherwise — so
  // the modal can draw a histogram instead of just a ring.
  metric: number | null;
}

// The scorecard keys the web knows how to drill into. Mirrors the `key` set in
// pipeline kpi_rollup.py. A tile whose key isn't here is simply not clickable.
export const KPI_KEYS = [
  "buy_side",
  "reached_senior_leadership",
  "founder_partner",
  "still_first_firm",
  "grad_degree",
  "years_to_senior_leadership",
  "tenure",
  "left_texas",
] as const;
export type KpiKey = (typeof KPI_KEYS)[number];

// One current-* value from claims (employer / title / location), or '' if absent.
function claimSub(claimType: string): string {
  return `COALESCE((SELECT c.value FROM claims c WHERE c.person_id = pi.person_id AND c.claim_type = '${claimType}' LIMIT 1), '')`;
}

// WHERE predicate + ORDER BY per KPI. Predicates read the per-person flags/metrics
// the pipeline already classified; ordering is chosen to make each list READ as an
// insight (fastest climbers first, longest tenure first, else alphabetical).
const KPI_QUERY: Record<KpiKey, { where: string; orderBy: string }> = {
  buy_side: { where: "pi.on_buy_side = 1", orderBy: "p.full_name" },
  reached_senior_leadership: { where: "pi.reached_senior_leadership = 1", orderBy: "p.full_name" },
  founder_partner: { where: "pi.founder_partner = 1", orderBy: "p.full_name" },
  still_first_firm: { where: "pi.still_first_firm = 1", orderBy: "p.full_name" },
  grad_degree: { where: "pi.has_advanced_degree = 1", orderBy: "p.full_name" },
  years_to_senior_leadership: {
    where: "pi.years_to_senior_leadership IS NOT NULL",
    orderBy: "pi.years_to_senior_leadership ASC, p.full_name",
  },
  tenure: { where: "pi.tenure_years IS NOT NULL", orderBy: "pi.tenure_years DESC, p.full_name" },
  left_texas: { where: "pi.left_texas = 1", orderBy: "p.full_name" },
};

interface KpiRow {
  name: string;
  slug: string;
  school: string;
  titanClass: number;
  employer: string;
  title: string;
  location: string;
  firstEmployer: string;
  yearsToSenior: number | null;
  tenureYears: number | null;
}

function joinRoleFirm(title: string, firm: string): string {
  if (title && firm) return `${title} · ${firm}`;
  return title || firm || "—";
}

// Compose the KPI-specific context line from the row. Kept in JS (not SQL) so each
// metric can present the most relevant fact without a thicket of CASE expressions.
function kpiDetail(key: KpiKey, r: KpiRow): string {
  switch (key) {
    case "buy_side":
    case "reached_senior_leadership":
    case "grad_degree":
      return joinRoleFirm(r.title, r.employer);
    case "founder_partner":
      return joinRoleFirm(r.title, r.employer);
    case "still_first_firm":
      return joinRoleFirm(r.title, r.firstEmployer || r.employer);
    case "years_to_senior_leadership":
      return r.yearsToSenior != null
        ? `${r.yearsToSenior} yrs to senior leadership${r.employer ? ` · ${r.employer}` : ""}`
        : joinRoleFirm(r.title, r.employer);
    case "tenure":
      return r.tenureYears != null
        ? `${r.tenureYears} ${r.tenureYears === 1 ? "yr" : "yrs"}${r.employer ? ` · ${r.employer}` : ""}`
        : joinRoleFirm(r.title, r.employer);
    case "left_texas":
      return r.location || r.employer || "—";
  }
}

// People behind one scorecard KPI, with a metric-specific context line. Returns []
// for an unknown key or before the pipeline has classified anyone.
export function kpiMembers(key: string): KpiMember[] {
  if (!(KPI_KEYS as readonly string[]).includes(key)) return [];
  const { where, orderBy } = KPI_QUERY[key as KpiKey];
  let rows: KpiRow[];
  try {
    rows = db()
      .prepare(
        `SELECT
           p.full_name   AS name,
           p.name_slug   AS slug,
           p.school      AS school,
           p.titan_class AS titanClass,
           ${claimSub("current_employer")} AS employer,
           ${claimSub("current_title")}    AS title,
           ${claimSub("location")}         AS location,
           COALESCE(pi.first_employer, '') AS firstEmployer,
           pi.years_to_senior_leadership AS yearsToSenior,
           pi.tenure_years AS tenureYears
         FROM person_insights pi
         JOIN people p ON p.id = pi.person_id
         WHERE ${where}
         ORDER BY ${orderBy}`
      )
      .all() as KpiRow[];
  } catch {
    return [];
  }
  return rows.map((r) => ({
    name: r.name,
    slug: r.slug,
    school: r.school,
    titanClass: r.titanClass,
    href: personHref(r.slug, r.titanClass),
    detail: kpiDetail(key as KpiKey, r),
    metric:
      key === "years_to_senior_leadership"
        ? r.yearsToSenior
        : key === "tenure"
          ? r.tenureYears
          : null,
  }));
}

// Per-person rows behind the FIRST-JOB sector card: the verified first employer
// (first jobs have no industry on record, so industry is left blank).
export function firstJobSectorMembers(): SectorMember[] {
  try {
    return db()
      .prepare(
        `SELECT
           pi.first_sector AS sector,
           p.full_name     AS name,
           p.name_slug     AS slug,
           p.school        AS school,
           p.titan_class   AS titanClass,
           COALESCE(pi.first_employer, '') AS employer,
           ''              AS industry
         FROM person_insights pi
         JOIN people p ON p.id = pi.person_id
         WHERE TRIM(COALESCE(pi.first_sector, '')) <> ''
         ORDER BY pi.first_sector, p.full_name`
      )
      .all()
      .map((r) => withHref(r as Omit<SectorMember, "href">));
  } catch {
    return [];
  }
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
// scalar columns plus the deserialized JSON payload drive the outcome half of
// the "Overview & Insights" view as soon as the snapshot reports at least one
// enriched person (see lib/insights.ts — the is_sample coverage flag is
// carried through but not gated on).
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
  signature_stats: { label: string; value: string; detail: string; pct: number; key: string }[];
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
// has run at least once. Until then this returns null and the Overview renders
// its empty states — never throws on the missing table.
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
    signature_stats?: { label: string; value: string; detail: string; pct: number; key?: string }[];
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
    signature_stats: (payload.signature_stats ?? []).map((s) => ({
      ...s,
      key: s.key ?? "",
    })),
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
