// Education parsing, institution canonicalization, and grouping.
//
// Raw education claims arrive in many shapes and with lots of redundancy:
//   "Bachelor of Business Administration From Texas A&M University"
//   "Texas A&M University, BBA in Finance, Magna Cum Laude"
//   "Mays Business School - Texas A&M University"
//   "Texas A&M University"            (a bare duplicate)
//   "CFA Charterholder"              (a credential, its own card)
//
// The render goal: one card per institution, every degree from that
// institution listed beneath it once, and no bare-institution card when a
// degreed card for the same school already exists.

import type { Claim } from "./db";
import { smartTitle } from "./normalize";

export interface EducationGroup {
  institution: string;
  degrees: string[];
  confidence: number;
  sourceUrl: string;
}

interface ParsedEducation {
  degree: string | null;
  institution: string;
  key: string; // canonical, lowercased — the grouping identity
  confidence: number;
  sourceUrl: string;
}

// Words that mark a string as a degree-granting institution.
const INSTITUTION_WORD = /\b(university|college|institute|polytechnic|academy|school)\b/i;
// Priority order when a string names both a sub-unit and its parent: a
// university/college outranks a "school" (e.g. "Mays Business School - Texas
// A&M University" -> "Texas A&M University").
const PARENT_WORD = /\b(university|college|institute|polytechnic|academy)\b/i;

// Minimal alias map for recurring sub-schools that belong to a parent
// institution in this (Texas-heavy) dataset, so they group under the parent
// instead of forming a duplicate card. Keyed by lowercased canonical name.
const CANONICAL_ALIASES: Record<string, string> = {
  "mays business school": "Texas A&M University",
};

// Strip a single trailing parenthetical — graduation years or honors that would
// otherwise pollute the institution / degree text, e.g. "... (2005-2009)" or
// "... (Summa Cum Laude)".
function stripTrailingParen(value: string): string {
  return value.replace(/\s*\([^)]*\)\s*$/, "").trim();
}

// Pick the institution out of a string that may bundle a sub-unit, an honors
// clause, or a parent school. Splits on the separators that join those parts
// and keeps the segment that most looks like a degree-granting institution.
function extractInstitution(raw: string): string {
  const cleaned = stripTrailingParen(raw);
  const segments = cleaned
    .split(/\s+-\s+|\s*\|\s*|\s+at\s+|,/i)
    .map((s) => s.trim())
    .filter(Boolean);
  if (segments.length <= 1) return cleaned;

  const parents = segments.filter((s) => PARENT_WORD.test(s));
  if (parents.length) return parents[parents.length - 1];

  const schools = segments.filter((s) => INSTITUTION_WORD.test(s));
  if (schools.length) return schools[schools.length - 1];

  return segments[0];
}

function canonicalize(rawInstitution: string): { display: string; key: string } {
  const institution = extractInstitution(rawInstitution);
  const titled = smartTitle(institution);
  const aliasKey = titled.toLowerCase();
  if (CANONICAL_ALIASES[aliasKey]) {
    const display = CANONICAL_ALIASES[aliasKey];
    return { display, key: display.toLowerCase() };
  }
  return { display: titled, key: aliasKey };
}

// Split one education claim value into a (degree, institution) pair, handling
// the "X from Y", "Y, X, honors", and bare-institution shapes.
function parseOne(claim: Claim): ParsedEducation {
  const raw = claim.value.trim();
  let degree: string | null = null;
  let institution = raw;

  const fromIdx = raw.toLowerCase().indexOf(" from ");
  if (fromIdx !== -1) {
    degree = raw.slice(0, fromIdx).trim();
    institution = raw.slice(fromIdx + 6).trim();
  } else if (raw.includes(",")) {
    const parts = raw.split(",").map((p) => p.trim()).filter(Boolean);
    const instIdx = parts.findIndex((p) => INSTITUTION_WORD.test(p));
    if (instIdx !== -1) {
      institution = parts[instIdx];
      const rest = parts.filter((_, i) => i !== instIdx).join(", ").trim();
      degree = rest || null;
    }
  }

  const { display, key } = canonicalize(institution);
  return {
    degree: degree ? smartTitle(stripTrailingParen(degree)) : null,
    institution: display,
    key,
    confidence: claim.confidence,
    sourceUrl: claim.source_url,
  };
}

// Fold canonical keys that are substrings of one another into the longer, more
// specific key (e.g. "texas a&m" -> "texas a&m university"). Returns a map from
// each raw key to its canonical winner.
function buildKeyMerges(keys: string[]): Map<string, string> {
  const unique = Array.from(new Set(keys));
  // Longest first so a short key resolves to its most specific container.
  const byLength = [...unique].sort((a, b) => b.length - a.length);
  const winner = new Map<string, string>();
  for (const key of unique) {
    const container = byLength.find(
      (other) => other !== key && other.includes(key)
    );
    winner.set(key, container ?? key);
  }
  return winner;
}

function dedupeDegrees(degrees: string[]): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const d of degrees) {
    const norm = d.toLowerCase().trim();
    if (!norm || seen.has(norm)) continue;
    seen.add(norm);
    out.push(d);
  }
  return out;
}

/**
 * Group education claims into one card per institution.
 *
 * Degrees from the same school collapse onto a single card (deduped,
 * case-insensitive). A bare-institution claim contributes no degree, so a
 * school that also has a degreed claim shows just the degree — never a
 * duplicate empty entry. Credentials and unmatched entries (no shared
 * institution) each stand as their own card. Cards order by confidence.
 */
export function groupEducation(claims: Claim[]): EducationGroup[] {
  const parsed = claims.map(parseOne);
  const merges = buildKeyMerges(parsed.map((p) => p.key));

  const order: string[] = [];
  const byKey = new Map<string, ParsedEducation[]>();
  for (const entry of parsed) {
    const key = merges.get(entry.key) ?? entry.key;
    const bucket = byKey.get(key);
    if (bucket) {
      bucket.push(entry);
    } else {
      byKey.set(key, [entry]);
      order.push(key);
    }
  }

  const groups = order.map((key) => {
    const entries = byKey.get(key)!;
    const best = entries.reduce((a, b) => (b.confidence > a.confidence ? b : a));
    // Prefer the display name from the highest-confidence entry; fall back to
    // the shortest (usually the cleanest/most canonical) name.
    const display =
      best.institution ||
      [...entries].sort((a, b) => a.institution.length - b.institution.length)[0]
        .institution;
    const degrees = dedupeDegrees(
      entries
        .filter((e) => e.degree)
        .sort((a, b) => b.confidence - a.confidence)
        .map((e) => e.degree as string)
    );
    const confidence = Math.max(...entries.map((e) => e.confidence));
    return { institution: display, degrees, confidence, sourceUrl: best.sourceUrl };
  });

  return groups.sort((a, b) => b.confidence - a.confidence);
}
