import {
  searchPeople as dbSearchPeople,
  peopleBySlugs,
  claimsForSlugs,
  type Person,
  type SlugClaim,
} from "@/lib/db";
import { semanticRankSlugs } from "./semantic";

// Typed search params the planner emits. `sector` is a slot for a future stored
// column; today it maps to the existing keyword OR-list in db.searchPeople.
export interface SearchParams {
  city?: string;
  school?: string;
  titanClass?: number;
  companyKeyword?: string;
  sector?: string;
  seniority?: string;
  intent?: string;
}

export interface RetrievedPerson {
  full_name: string;
  name_slug: string;
  titan_class: number;
  school: string;
  initial_company: string;
  city: string;
  source_url: string;
  claims: { claim_type: string; value: string; source_url: string }[];
}

const RESULT_LIMIT = 12;
const SEMANTIC_K = 8;

function toRetrieved(r: Person, claims: SlugClaim[]): RetrievedPerson {
  return {
    full_name: r.full_name,
    name_slug: r.name_slug,
    titan_class: r.titan_class,
    school: r.school,
    initial_company: r.initial_company,
    city: r.city,
    source_url: r.source_url,
    claims: claims.map((c) => ({
      claim_type: c.claim_type,
      value: c.value,
      source_url: c.source_url,
    })),
  };
}

// Fold source-attributed claims onto a set of Person rows, preserving row order.
function foldClaims(rows: Person[]): RetrievedPerson[] {
  if (rows.length === 0) return [];
  const claims = claimsForSlugs(rows.map((r) => r.name_slug));
  const bySlug = new Map<string, SlugClaim[]>();
  for (const c of claims) {
    bySlug.set(c.name_slug, [...(bySlug.get(c.name_slug) ?? []), c]);
  }
  return rows.map((r) => toRetrieved(r, bySlug.get(r.name_slug) ?? []));
}

function hasStructured(p: SearchParams): boolean {
  return Boolean(
    p.city || p.school || p.titanClass || p.companyKeyword || p.sector || p.seniority
  );
}

// Keyword/facet retrieval only (no model). Kept for callers and tests that want
// the deterministic SQL path on its own.
export function searchPeople(params: SearchParams): RetrievedPerson[] {
  const rows = dbSearchPeople({
    city: params.city,
    school: params.school,
    titanClass: params.titanClass,
    companyKeyword: params.companyKeyword,
    sector: params.sector,
    seniority: params.seniority,
    limit: RESULT_LIMIT,
  });
  return foldClaims(rows);
}

// Hybrid retrieval: keyword/facet SQL + semantic vector search, merged.
//   - When the planner extracted explicit filters (city/sector/seniority/…), those
//     are the visitor's stated constraints, so keyword rows LEAD and semantic
//     supplements any open slots.
//   - For a pure natural-language question (no structured filters), SEMANTIC leads
//     — meaning beats the generic "most-enriched" keyword fallback.
// Semantic degrades to [] when vectors/model are unavailable, so this can never do
// worse than the keyword path alone.
export async function retrievePeople(
  params: SearchParams,
  queryText: string
): Promise<RetrievedPerson[]> {
  const keywordRows = dbSearchPeople({
    city: params.city,
    school: params.school,
    titanClass: params.titanClass,
    companyKeyword: params.companyKeyword,
    sector: params.sector,
    seniority: params.seniority,
    limit: RESULT_LIMIT,
  });
  const semSlugs = await semanticRankSlugs(queryText, SEMANTIC_K);
  const semRows = semSlugs.length ? peopleBySlugs(semSlugs) : [];

  const seen = new Set<string>();
  const merged: Person[] = [];
  const push = (rows: Person[]) => {
    for (const r of rows) {
      if (!seen.has(r.name_slug)) {
        seen.add(r.name_slug);
        merged.push(r);
      }
    }
  };
  if (hasStructured(params)) {
    push(keywordRows);
    push(semRows);
  } else {
    push(semRows);
    push(keywordRows);
  }

  return foldClaims(merged.slice(0, RESULT_LIMIT));
}
