import type { Claim, Person } from "./db";
import { smartTitle } from "./normalize";
import { groupEducation, type EducationGroup } from "./education";
import { usefulLinks } from "./link-quality";

export interface ExperienceEntry {
  title: string;
  company: string;
  start: string | null;
  end: string | null;
  current: boolean;
  confidence: number;
  sourceUrl: string;
}

// One company block in the timeline. A solo role is just a group of one; a
// company where the person held several roles renders LinkedIn-style: company
// name + overall tenure once, with the individual dated roles nested beneath.
export interface ExperienceGroup {
  company: string;
  start: string | null;
  end: string | null;
  current: boolean;
  roles: ExperienceEntry[];
}

export interface ResumeLink {
  label: string;
  url: string;
}

// An unverified public news mention. A GNews name-search can return a namesake,
// so these are kept OUT of the verified résumé and its stats and rendered in a
// clearly-labeled, separate section.
export interface NewsItem {
  headline: string;
  date: string | null;
  url: string;
  snippet: string;
}

export interface Resume {
  currentTitle: string | null;
  currentEmployer: string | null;
  location: string | null;
  bio: string | null;
  linkedinUrl: string | null;
  experience: ExperienceEntry[];
  experienceGroups: ExperienceGroup[];
  education: EducationGroup[];
  links: ResumeLink[];
  news: NewsItem[];
  sources: string[];
  claimCount: number;
  avgConfidence: number;
}

const NOW_TOKENS = new Set(["now", "present", "current"]);

// Non-place values that have leaked into location claims and must never render.
const JUNK_LOCATIONS = new Set([
  "true", "false", "yes", "no", "n/a", "na", "unknown", "none", "null",
]);

function normalizeEnd(raw: string | null): { end: string | null; current: boolean } {
  if (!raw) return { end: null, current: false };
  if (NOW_TOKENS.has(raw.trim().toLowerCase())) return { end: "Present", current: true };
  return { end: raw.trim(), current: false };
}

// Year-range separators seen across sources: hyphen, en-dash (–), em-dash (—).
// career_history quote: "2018 - 2020 Senior Investment Manager @ Company"
const QUOTE_RE = /^(now|present|\d{4})\s*[-–—]\s*(now|present|\d{4})\s+(.*?)\s+@\s+(.+)$/i;
// career_history value: "Title at Company (2018-2020)"
const VALUE_RE = /^(.+?)\s+at\s+(.+?)\s*\((now|present|\d{4})\s*[-–—]\s*(now|present|\d{4})\)\s*$/i;
// "Title at Company (2019)" — single-year value form.
const SINGLE_YEAR_RE = /^(.+?)\s+at\s+(.+?)\s*\((now|present|\d{4})\)\s*$/i;
// "Title at Company" — dateless narrative form (a prose claim with no parsable
// date range). Parsed so the company is known and the entry can dedup/group
// with its dated twin instead of floating as a bare title.
const ATONLY_RE = /^(.+?)\s+at\s+(.+)$/i;

// Drop a trailing year parenthetical — "(2019)" / "(2019-2020)" — but keep a
// descriptive one like "(Teacher Retirement System of Texas)".
function stripYearParen(value: string): string {
  return value
    .replace(/\s*\((?:19|20)\d{2}(?:\s*[-–—]\s*(?:(?:19|20)\d{2}|present|now))?\)\s*$/i, "")
    .trim();
}

function parseExperience(claim: Claim): ExperienceEntry {
  // Title/company are title-cased on the way out: the quote is captured
  // verbatim (often lowercase) and even the normalized value can carry source
  // quirks ("Kbre", "Llc"), so smartTitle is the render-time guarantee.
  const fromQuote = claim.quote?.match(QUOTE_RE);
  if (fromQuote) {
    const [, start, end, title, company] = fromQuote;
    const norm = normalizeEnd(end);
    return {
      title: smartTitle(title.trim()),
      company: smartTitle(company.trim()),
      start: start.trim(),
      ...norm,
      confidence: claim.confidence,
      sourceUrl: claim.source_url,
    };
  }
  const fromValue = claim.value.match(VALUE_RE);
  if (fromValue) {
    const [, title, company, start, end] = fromValue;
    const norm = normalizeEnd(end);
    return {
      title: smartTitle(title.trim()),
      company: smartTitle(company.trim()),
      start: start.trim(),
      ...norm,
      confidence: claim.confidence,
      sourceUrl: claim.source_url,
    };
  }
  const singleYear = claim.value.match(SINGLE_YEAR_RE);
  if (singleYear) {
    const [, title, company, year] = singleYear;
    const norm = normalizeEnd(year);
    return {
      title: smartTitle(title.trim()),
      company: smartTitle(company.trim()),
      start: norm.current ? null : year.trim(),
      ...norm,
      confidence: claim.confidence,
      sourceUrl: claim.source_url,
    };
  }
  const atOnly = claim.value.match(ATONLY_RE);
  if (atOnly) {
    const [, title, company] = atOnly;
    return {
      title: smartTitle(title.trim()),
      company: smartTitle(stripYearParen(company.trim())),
      start: null,
      end: null,
      current: false,
      confidence: claim.confidence,
      sourceUrl: claim.source_url,
    };
  }
  return {
    title: smartTitle(claim.value),
    company: "",
    start: null,
    end: null,
    current: false,
    confidence: claim.confidence,
    sourceUrl: claim.source_url,
  };
}

function sortKey(e: ExperienceEntry): number {
  // Current roles sort above all past roles; among multiple current roles, the
  // most recently-started one ranks highest (so it wins as the displayed title).
  if (e.current) {
    const start = Number(e.start ?? 0);
    return 9999 + (Number.isNaN(start) ? 0 : start);
  }
  const year = Number(e.end ?? e.start ?? 0);
  return Number.isNaN(year) ? 0 : year;
}

function yearOf(raw: string | null): number | null {
  if (!raw) return null;
  const n = Number(raw);
  return Number.isNaN(n) ? null : n;
}

// Lowercased, punctuation-stripped tokens — the basis for comparing titles and
// companies that differ only in case/punctuation ("Och-Ziff" vs "Och-ziff").
function normalizeText(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

// How much date information an entry carries: a dated/current entry is richer
// than one with only a start, which is richer than a dateless prose entry.
function dateRank(e: ExperienceEntry): number {
  if (e.current || e.end) return 2;
  if (e.start) return 1;
  return 0;
}

// Remove duplicate roles that the same person picked up from two sources — a
// dated structured claim and a dateless prose claim describing the same job.
// Two entries collide on (title, company); the richer one (more date info, then
// higher confidence) wins. Order of first appearance is preserved.
function dedupeExperience(entries: ExperienceEntry[]): ExperienceEntry[] {
  const best = new Map<string, ExperienceEntry>();
  const order: string[] = [];
  for (const e of entries) {
    const key = `${normalizeText(e.title)}|${normalizeText(e.company)}`;
    const current = best.get(key);
    if (!current) {
      best.set(key, e);
      order.push(key);
      continue;
    }
    const richer =
      dateRank(e) > dateRank(current) ||
      (dateRank(e) === dateRank(current) && e.confidence > current.confidence);
    if (richer) best.set(key, e);
  }
  return order.map((k) => best.get(k)!);
}

// Two titles are the same role when one is a contiguous-token run of the other
// ("Investment Banking Analyst" inside "Investment Banking Analyst in the FIG")
// or they are identical after normalization.
function titlesCompatible(a: string, b: string): boolean {
  const na = normalizeText(a);
  const nb = normalizeText(b);
  if (!na || !nb) return false;
  if (na === nb) return true;
  const ta = na.split(" ");
  const tb = nb.split(" ");
  return isContiguousTokenRun(ta, tb) || isContiguousTokenRun(tb, ta);
}

// Two company strings refer to the same employer when one contains the other as
// a substring after normalization ("citi" inside "citigroup in new york",
// "berkshire partners" inside "berkshire partners lp").
function companiesCompatible(a: string, b: string): boolean {
  const na = normalizeText(a);
  const nb = normalizeText(b);
  if (!na || !nb) return false;
  return na === nb || na.includes(nb) || nb.includes(na);
}

function dateSig(e: ExperienceEntry): string {
  return `${e.start ?? ""}|${e.end ?? ""}|${e.current}`;
}

// Collapse two entries that are the SAME real role described by two sources with
// different phrasing — same date range, compatible titles, compatible companies
// (e.g. PDL's "Investment Banking Analyst @ Citi (2015-2017)" and Firecrawl's
// "Investment Banking Analyst in the Financial Institutions Group @ Citigroup in
// New York (2015-2017)"). Only DATED roles merge (start present), so a dateless
// "Associate" never swallows a dateless "Senior Associate". The merged entry
// keeps the more specific title, the cleaner (shorter) company, and the higher
// confidence. Exact (title, company) dupes are already gone via dedupeExperience.
function coalesceSameRole(entries: ExperienceEntry[]): ExperienceEntry[] {
  const result: ExperienceEntry[] = [];
  for (const e of entries) {
    if (!e.start) {
      result.push(e);
      continue;
    }
    const i = result.findIndex(
      (r) =>
        Boolean(r.start) &&
        dateSig(r) === dateSig(e) &&
        titlesCompatible(r.title, e.title) &&
        companiesCompatible(r.company, e.company)
    );
    if (i === -1) {
      result.push(e);
      continue;
    }
    const r = result[i];
    result[i] = {
      ...r,
      title: e.title.length > r.title.length ? e.title : r.title,
      company:
        e.company.length < r.company.length && e.company
          ? e.company
          : r.company,
      confidence: Math.max(r.confidence, e.confidence),
    };
  }
  return result;
}

// Drop a dateless prose entry that has no parsed company but names — in its
// text — a company already covered by a dated role. "Investment Banking Division
// of Lehman Brothers" is dropped when "...at Lehman Brothers (2006-2009)" exists,
// while "Chief Financial Officer of Falcon Minerals" survives because Falcon
// appears nowhere else (it is real, additional history).
function dropRedundantProse(entries: ExperienceEntry[]): ExperienceEntry[] {
  const datedCompanies = entries
    .filter((e) => e.company && dateRank(e) > 0)
    .map((e) => normalizeText(e.company))
    .filter((c) => c.split(" ").length >= 2 && c.length >= 6);

  return entries.filter((e) => {
    const isProse = !e.company && dateRank(e) === 0;
    if (!isProse) return true;
    const text = normalizeText(e.title).split(" ");
    return !datedCompanies.some((c) => isContiguousTokenRun(c.split(" "), text));
  });
}

// True when `short` appears as a contiguous run of tokens inside `long` — used
// to fold company-name variants together ("Teacher Retirement System of Texas"
// inside "Texas Teachers (Teacher Retirement System of Texas)"). Guarded to ≥2
// tokens / ≥6 chars so generic fragments ("capital", "partners") never merge
// two genuinely different firms.
function isContiguousTokenRun(short: string[], long: string[]): boolean {
  if (short.length < 2 || short.join("").length < 6) return false;
  if (short.length > long.length) return false;
  for (let i = 0; i + short.length <= long.length; i += 1) {
    if (short.every((tok, j) => long[i + j] === tok)) return true;
  }
  return false;
}

// Map each distinct company key to a canonical winner, folding any key that is a
// contiguous token-run of another into the longer (more specific) one.
function mergeCompanyKeys(keys: string[]): Map<string, string> {
  const unique = Array.from(new Set(keys));
  const winner = new Map<string, string>();
  for (const key of unique) {
    const longer = unique.find(
      (other) =>
        other !== key &&
        other.length > key.length &&
        isContiguousTokenRun(key.split(" "), other.split(" "))
    );
    winner.set(key, longer ?? key);
  }
  return winner;
}

// Collapse roles at the same employer into one group so a person's multiple
// stints read as one company with nested roles (LinkedIn-style) rather than the
// company name repeating on every row. Empty-company entries never merge — each
// stays its own group. Roles within a group sort newest-first; groups order by
// their newest role so a grouped company keeps its place in the timeline.
function groupExperience(entries: ExperienceEntry[]): ExperienceGroup[] {
  const merges = mergeCompanyKeys(
    entries.map((e) => normalizeText(e.company)).filter(Boolean)
  );

  const order: string[] = [];
  const byKey = new Map<string, ExperienceEntry[]>();

  entries.forEach((e, i) => {
    const norm = normalizeText(e.company);
    // Solo / unknown-company entries get a unique key so they never coalesce.
    const key = norm ? `co:${merges.get(norm) ?? norm}` : `solo:${i}`;
    const bucket = byKey.get(key);
    if (bucket) {
      bucket.push(e);
    } else {
      byKey.set(key, [e]);
      order.push(key);
    }
  });

  const groups = order.map((key) => {
    const roles = byKey.get(key)!.slice().sort((a, b) => sortKey(b) - sortKey(a));
    const starts = roles.map((r) => yearOf(r.start)).filter((y): y is number => y !== null);
    const ends = roles.map((r) => yearOf(r.end)).filter((y): y is number => y !== null);
    const current = roles.some((r) => r.current);
    return {
      company: roles[0].company,
      start: starts.length ? String(Math.min(...starts)) : null,
      end: current ? "Present" : ends.length ? String(Math.max(...ends)) : null,
      current,
      roles,
    };
  });

  const groupKey = (g: ExperienceGroup): number =>
    Math.max(...g.roles.map(sortKey));
  return groups.sort((a, b) => groupKey(b) - groupKey(a));
}

function firstValue(claims: Claim[], type: string): Claim | undefined {
  return claims.find((c) => c.claim_type === type);
}

// Split an employer packed into the title ("Consultant at Boston Consulting Group")
// into [title, employer] — but ONLY when no separate employer was captured. Splits
// on the LAST " at " so titles like "Head of M&A at Apollo" resolve correctly, and
// bails (keeps the title whole) if either side would be empty.
export function splitTitleEmployer(
  titleRaw: string | null,
  employerRaw: string | null
): [string | null, string | null] {
  if (employerRaw && employerRaw.trim()) return [titleRaw, employerRaw];
  if (!titleRaw) return [titleRaw, employerRaw];
  const m = titleRaw.match(/^(.*\S)\s+at\s+(\S.*)$/i);
  if (!m) return [titleRaw, employerRaw];
  return [m[1].trim(), m[2].trim()];
}

function titleMatchesAny(title: string, entries: ExperienceEntry[]): boolean {
  const t = normalizeText(title);
  if (!t) return false;
  return entries.some((e) => {
    const et = normalizeText(e.title);
    return Boolean(et) && (et === t || et.includes(t) || t.includes(et));
  });
}

// Trust the dated timeline over a stale standalone current_title. Fires ONLY when
// the current employer has a role marked current (present) AND the current_title
// matches no role at that employer — the signature of a mismatched/stale title
// (PDL/scrape lag) like "PhD Student" paired with employer "Facteus" while the
// timeline shows "VP Marketing at Facteus, present". Otherwise the title is kept.
export function reconcileCurrentTitle(
  currentTitle: string | null,
  currentEmployer: string | null,
  history: ExperienceEntry[]
): string | null {
  if (!currentEmployer) return currentTitle;
  const emp = normalizeText(currentEmployer);
  if (!emp) return currentTitle;
  const atEmployer = history.filter((e) => {
    const co = normalizeText(e.company);
    return Boolean(co) && (co === emp || co.includes(emp) || emp.includes(co));
  });
  const currentAtEmployer = atEmployer.filter((e) => e.current);
  if (!currentAtEmployer.length) return currentTitle;
  if (currentTitle && titleMatchesAny(currentTitle, atEmployer)) return currentTitle;
  // Use the most-recent current role's title at this employer.
  const best = currentAtEmployer.slice().sort((a, b) => sortKey(b) - sortKey(a))[0];
  return best.title || currentTitle;
}

// The headline role lives in current_title / current_employer claims, separate
// from career_history. Build it as a synthetic timeline entry so the person's
// actual current job leads their experience — unless career_history already
// covers it.
function currentRoleEntry(
  claims: Claim[],
  currentTitle: string | null,
  currentEmployer: string | null,
  existing: ExperienceEntry[]
): ExperienceEntry | null {
  if (!currentTitle && !currentEmployer) return null;

  const employerLc = currentEmployer?.toLowerCase() ?? null;
  const titleLc = currentTitle?.toLowerCase() ?? "";
  const alreadyCovered = existing.some((e) => {
    if (!e.current) return false;
    const co = e.company.toLowerCase();
    if (employerLc && co && (co.includes(employerLc) || employerLc.includes(co))) {
      return true;
    }
    return Boolean(employerLc) && titleLc.includes(employerLc as string);
  });
  if (alreadyCovered) return null;

  const src = firstValue(claims, "current_title") ?? firstValue(claims, "current_employer");
  return {
    title: smartTitle(currentTitle ?? ""),
    company: smartTitle(currentEmployer ?? ""),
    start: null,
    end: null,
    current: true,
    confidence: src?.confidence ?? 0,
    sourceUrl: src?.source_url ?? "",
  };
}

function findLinkedIn(claims: Claim[]): string | null {
  // Restrict to public_links claims only — searching all claim types risks
  // matching a news_mention that references linkedin.com in a different context
  // (e.g. an article about a different person at LinkedIn Inc.).
  for (const c of claims.filter((c) => c.claim_type === "public_links")) {
    const haystack = `${c.value} ${c.source_url}`.toLowerCase();
    if (haystack.includes("linkedin.com")) {
      const match = `${c.value} ${c.source_url}`.match(
        /https?:\/\/[^\s)"]*linkedin\.com\/[^\s)"]*/i
      );
      if (match) return match[0];
    }
  }
  return null;
}

export function linkedinSearchUrl(person: Person): string {
  const terms = [person.full_name, person.initial_company]
    .filter((t) => t && t !== "(unknown)")
    .join(" ");
  return `https://www.linkedin.com/search/results/people/?keywords=${encodeURIComponent(terms)}`;
}

// News mentions come from an unverified name-search (GNews), so they are kept
// out of every verified-résumé statistic. Only these aggregate over the full
// claim list; the verified résumé below uses `verified` exclusively.
function parseNews(claim: Claim): NewsItem {
  const idx = claim.value.indexOf(" — ");
  const hasDate = idx !== -1;
  return {
    headline: hasDate ? claim.value.slice(idx + 3).trim() : claim.value.trim(),
    date: hasDate ? claim.value.slice(0, idx).trim() : null,
    url: claim.source_url,
    snippet: claim.quote ?? "",
  };
}

export function buildResume(person: Person, claims: Claim[]): Resume {
  // Verified-résumé facts exclude unverified news mentions entirely.
  const verified = claims.filter((c) => c.claim_type !== "news_mention");

  const news: NewsItem[] = claims
    .filter((c) => c.claim_type === "news_mention")
    .map(parseNews);

  const currentTitleRaw = firstValue(verified, "current_title")?.value ?? null;
  const currentEmployerRaw = firstValue(verified, "current_employer")?.value ?? null;
  // When a source packs the employer into the title ("Consultant at Boston
  // Consulting Group") and left the employer field empty, split it so the title
  // and employer render cleanly. Only split when we have no employer already.
  const [splitTitle, splitEmployer] = splitTitleEmployer(
    currentTitleRaw,
    currentEmployerRaw
  );
  const currentTitle = splitTitle ? smartTitle(splitTitle) : null;
  const currentEmployer = splitEmployer ? smartTitle(splitEmployer) : null;

  const careerHistory = verified
    .filter((c) => c.claim_type === "career_history")
    .map(parseExperience);

  // Reconcile a stale/mismatched current_title against the dated career history:
  // when a CURRENT role at the current employer contradicts a current_title that
  // matches no role there (e.g. PDL employer "Facteus" + a scraped "PhD Student"
  // title, while the timeline shows "VP Marketing at Facteus, present"), trust the
  // dated entry. Conservative: only fires on a present-dated role at that employer.
  const reconciledTitle = reconcileCurrentTitle(
    currentTitle,
    currentEmployer,
    careerHistory
  );

  const current = currentRoleEntry(verified, reconciledTitle, currentEmployer, careerHistory);
  const combined = current ? [current, ...careerHistory] : careerHistory;
  const experience = dropRedundantProse(
    coalesceSameRole(dedupeExperience(combined))
  ).sort((a, b) => sortKey(b) - sortKey(a));

  const education = groupEducation(
    verified.filter((c) => c.claim_type === "education")
  );

  // public_links is overloaded — genuine appearances plus data-broker pages,
  // social noise, filings, and bare bio headings. Show only the useful ones
  // (drops directories/brokers/boilerplate/name-only bios + the redundant
  // LinkedIn, which is the header button). See lib/link-quality.ts.
  const links: ResumeLink[] = usefulLinks(
    verified
      .filter((c) => c.claim_type === "public_links")
      .map((c) => ({ label: c.value, url: c.source_url })),
    person.full_name
  );

  // Some enrichment runs leaked a boolean into the location field ("True").
  // Skip junk values so a real city (or the directory city) is used instead.
  const locationClaim = verified.find(
    (c) =>
      c.claim_type === "location" &&
      !JUNK_LOCATIONS.has(c.value.trim().toLowerCase())
  );
  const locationRaw =
    locationClaim?.value ??
    (person.city && person.city !== "(unknown)" ? person.city : null);
  const location = locationRaw ? smartTitle(locationRaw) : null;

  const sources = Array.from(
    new Set(verified.map((c) => c.source_url).filter(Boolean))
  );

  const avgConfidence = verified.length
    ? verified.reduce((sum, c) => sum + c.confidence, 0) / verified.length
    : 0;

  return {
    currentTitle: reconciledTitle,
    currentEmployer,
    location,
    bio: firstValue(verified, "short_bio")?.value ?? null,
    linkedinUrl: findLinkedIn(verified),
    experience,
    experienceGroups: groupExperience(experience),
    education,
    links,
    news,
    sources,
    claimCount: verified.length,
    avgConfidence,
  };
}
