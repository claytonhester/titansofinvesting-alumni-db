import { loadPersonVectors, type PersonVector } from "@/lib/db";

// Semantic retrieval for the chat: embed the visitor's question with the SAME
// in-process model used to build person_vectors (all-MiniLM-L6-v2, 384-dim, no
// API/vendor), then rank people by cosine similarity. Vectors are normalized at
// build time, so a normalized query embedding makes cosine a plain dot product.
//
// Everything here is server-only (transformers.js + better-sqlite3). It degrades
// to [] — never throws — when the model can't load or vectors aren't built yet,
// so the caller falls back to keyword/facet search.

const MODEL = "Xenova/all-MiniLM-L6-v2";

// Lazily-loaded, process-wide singletons. The model load (~2s cold) happens once;
// the vector set is small (~1.6MB for 1k people) and cached after first read.
let extractorPromise: Promise<unknown> | null = null;
let vectorCache: PersonVector[] | null = null;

async function getExtractor(): Promise<((text: string, opts: object) => Promise<{ data: Float32Array }>) | null> {
  if (!extractorPromise) {
    extractorPromise = import("@xenova/transformers")
      .then((m) => m.pipeline("feature-extraction", MODEL))
      .catch((err) => {
        // Reset so a later call can retry, and signal failure to callers.
        extractorPromise = null;
        throw err;
      });
  }
  try {
    return (await extractorPromise) as (text: string, opts: object) => Promise<{ data: Float32Array }>;
  } catch {
    return null;
  }
}

function vectors(): PersonVector[] {
  if (vectorCache === null) vectorCache = loadPersonVectors();
  return vectorCache;
}

// Visible for tests / explicit refresh after a re-embed within a long-lived process.
export function resetSemanticCache(): void {
  vectorCache = null;
}

async function embedQuery(query: string): Promise<Float32Array | null> {
  const extractor = await getExtractor();
  if (!extractor) return null;
  try {
    const out = await extractor(query, { pooling: "mean", normalize: true });
    return out.data;
  } catch {
    return null;
  }
}

function dot(a: Float32Array, b: Float32Array): number {
  const n = Math.min(a.length, b.length);
  let s = 0;
  for (let i = 0; i < n; i++) s += a[i] * b[i];
  return s;
}

// Top-k name_slugs by cosine similarity to the query. Returns [] (not an error)
// when embeddings are unavailable, so retrieval falls back to keyword/facet search.
export async function semanticRankSlugs(
  query: string,
  k = 8,
  minScore = 0.2
): Promise<string[]> {
  const trimmed = (query || "").trim();
  if (!trimmed) return [];
  const rows = vectors();
  if (rows.length === 0) return [];
  const q = await embedQuery(trimmed);
  if (!q) return [];

  const scored = rows
    .map((r) => ({ slug: r.name_slug, score: dot(q, r.vec) }))
    .filter((s) => s.score >= minScore)
    .sort((a, b) => b.score - a.score)
    .slice(0, k);
  return scored.map((s) => s.slug);
}
