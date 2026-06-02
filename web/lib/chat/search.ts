import {
  searchPeople as dbSearchPeople,
  claimsForSlugs,
  type Person,
  type SlugClaim,
} from "@/lib/db";

// Typed search params the planner emits. `sector` is a slot for a future stored
// column; today it maps to the existing keyword OR-list in db.searchPeople.
export interface SearchParams {
  city?: string;
  school?: string;
  titanClass?: number;
  companyKeyword?: string;
  sector?: string;
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

// Pure-ish retrieval: delegates all SQL to the read-only db layer, then folds
// source-attributed claims onto each row. Bounded result set. No model call.
export function searchPeople(params: SearchParams): RetrievedPerson[] {
  const rows: Person[] = dbSearchPeople({
    city: params.city,
    school: params.school,
    titanClass: params.titanClass,
    companyKeyword: params.companyKeyword,
    sector: params.sector,
    limit: RESULT_LIMIT,
  });

  if (rows.length === 0) return [];

  const slugs = rows.map((r) => r.name_slug);
  const claims: SlugClaim[] = claimsForSlugs(slugs);
  const bySlug = new Map<string, SlugClaim[]>();
  for (const c of claims) {
    bySlug.set(c.name_slug, [...(bySlug.get(c.name_slug) ?? []), c]);
  }

  return rows.map((r) => ({
    full_name: r.full_name,
    name_slug: r.name_slug,
    titan_class: r.titan_class,
    school: r.school,
    initial_company: r.initial_company,
    city: r.city,
    source_url: r.source_url,
    claims: (bySlug.get(r.name_slug) ?? []).map((c) => ({
      claim_type: c.claim_type,
      value: c.value,
      source_url: c.source_url,
    })),
  }));
}
