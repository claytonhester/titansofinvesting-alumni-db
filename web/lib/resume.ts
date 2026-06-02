import type { Claim, Person } from "./db";

export interface ExperienceEntry {
  title: string;
  company: string;
  start: string | null;
  end: string | null;
  current: boolean;
  confidence: number;
  sourceUrl: string;
}

export interface EducationEntry {
  degree: string | null;
  institution: string;
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
  education: EducationEntry[];
  links: ResumeLink[];
  news: NewsItem[];
  sources: string[];
  claimCount: number;
  avgConfidence: number;
}

const NOW_TOKENS = new Set(["now", "present", "current"]);

function normalizeEnd(raw: string | null): { end: string | null; current: boolean } {
  if (!raw) return { end: null, current: false };
  if (NOW_TOKENS.has(raw.trim().toLowerCase())) return { end: "Present", current: true };
  return { end: raw.trim(), current: false };
}

// career_history quote: "2018 - 2020 Senior Investment Manager @ Company"
const QUOTE_RE = /^(now|present|\d{4})\s*[-–]\s*(now|present|\d{4})\s+(.*?)\s+@\s+(.+)$/i;
// career_history value: "Title at Company (2018-2020)"
const VALUE_RE = /^(.+?)\s+at\s+(.+?)\s*\((now|present|\d{4})\s*[-–]\s*(now|present|\d{4})\)\s*$/i;

function parseExperience(claim: Claim): ExperienceEntry {
  const fromQuote = claim.quote?.match(QUOTE_RE);
  if (fromQuote) {
    const [, start, end, title, company] = fromQuote;
    const norm = normalizeEnd(end);
    return {
      title: title.trim(),
      company: company.trim(),
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
      title: title.trim(),
      company: company.trim(),
      start: start.trim(),
      ...norm,
      confidence: claim.confidence,
      sourceUrl: claim.source_url,
    };
  }
  return {
    title: claim.value,
    company: "",
    start: null,
    end: null,
    current: false,
    confidence: claim.confidence,
    sourceUrl: claim.source_url,
  };
}

function sortKey(e: ExperienceEntry): number {
  if (e.current) return 9999;
  const year = Number(e.end ?? e.start ?? 0);
  return Number.isNaN(year) ? 0 : year;
}

function yearOf(raw: string | null): number | null {
  if (!raw) return null;
  const n = Number(raw);
  return Number.isNaN(n) ? null : n;
}

// Collapse roles at the same employer into one group so a person's multiple
// stints read as one company with nested roles (LinkedIn-style) rather than the
// company name repeating on every row. Empty-company entries never merge — each
// stays its own group. Roles within a group sort newest-first; groups order by
// their newest role so a grouped company keeps its place in the timeline.
function groupExperience(entries: ExperienceEntry[]): ExperienceGroup[] {
  const order: string[] = [];
  const byKey = new Map<string, ExperienceEntry[]>();

  entries.forEach((e, i) => {
    const co = e.company.trim();
    // Solo / unknown-company entries get a unique key so they never coalesce.
    const key = co ? `co:${co.toLowerCase()}` : `solo:${i}`;
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

function parseEducation(claim: Claim): EducationEntry {
  const idx = claim.value.toLowerCase().indexOf(" from ");
  if (idx !== -1) {
    return {
      degree: claim.value.slice(0, idx).trim(),
      institution: claim.value.slice(idx + 6).trim(),
      confidence: claim.confidence,
      sourceUrl: claim.source_url,
    };
  }
  return {
    degree: null,
    institution: claim.value.trim(),
    confidence: claim.confidence,
    sourceUrl: claim.source_url,
  };
}

function firstValue(claims: Claim[], type: string): Claim | undefined {
  return claims.find((c) => c.claim_type === type);
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
    title: currentTitle ?? "",
    company: currentEmployer ?? "",
    start: null,
    end: null,
    current: true,
    confidence: src?.confidence ?? 0,
    sourceUrl: src?.source_url ?? "",
  };
}

function findLinkedIn(claims: Claim[]): string | null {
  for (const c of claims) {
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

  const currentTitle = firstValue(verified, "current_title")?.value ?? null;
  const currentEmployer = firstValue(verified, "current_employer")?.value ?? null;

  const careerHistory = verified
    .filter((c) => c.claim_type === "career_history")
    .map(parseExperience);

  const current = currentRoleEntry(verified, currentTitle, currentEmployer, careerHistory);
  const experience = (current ? [current, ...careerHistory] : careerHistory).sort(
    (a, b) => sortKey(b) - sortKey(a)
  );

  const education = verified
    .filter((c) => c.claim_type === "education")
    .map(parseEducation)
    .sort((a, b) => b.confidence - a.confidence);

  const links: ResumeLink[] = verified
    .filter((c) => c.claim_type === "public_links")
    .map((c) => ({ label: c.value, url: c.source_url }));

  const locationClaim = firstValue(verified, "location");
  const location =
    locationClaim?.value ??
    (person.city && person.city !== "(unknown)" ? person.city : null);

  const sources = Array.from(
    new Set(verified.map((c) => c.source_url).filter(Boolean))
  );

  const avgConfidence = verified.length
    ? verified.reduce((sum, c) => sum + c.confidence, 0) / verified.length
    : 0;

  return {
    currentTitle,
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
