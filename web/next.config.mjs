/** @type {import('next').NextConfig} */
const nextConfig = {
  // Native / heavy modules kept external to the server bundle: better-sqlite3
  // (native addon) and the in-process embedding model (onnxruntime backend).
  serverExternalPackages: ["better-sqlite3", "@xenova/transformers"],
  // Pin the workspace root so Turbopack ignores lockfiles further up the tree.
  turbopack: { root: import.meta.dirname },
  // Every server route reads ./data/titans.db via an fs path at runtime, which
  // Next's dependency tracing can't infer — so the snapshot is force-included
  // into each function bundle (pipeline/ is not deployed to Vercel).
  // IMPORTANT: the production build runs with webpack (`next build --webpack`),
  // because Next 16 defaults `build` to Turbopack, which does NOT honor these
  // includes — under Turbopack the DB never shipped and every route 500'd with
  // "unable to open database file".
  outputFileTracingIncludes: {
    "/": ["./data/titans.db"],
    "/api/chat": ["./data/titans.db"],
    "/person/[slug]": ["./data/titans.db"],
    "/company/[slug]": ["./data/titans.db"],
  },
};

export default nextConfig;
