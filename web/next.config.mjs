/** @type {import('next').NextConfig} */
const nextConfig = {
  // Native / heavy modules kept external to the server bundle: better-sqlite3
  // (native addon) and the in-process embedding model (onnxruntime backend).
  serverExternalPackages: ["better-sqlite3", "@xenova/transformers"],
  // Pin the workspace root so Turbopack ignores lockfiles further up the tree.
  turbopack: { root: import.meta.dirname },
  // Every server route reads the SQLite snapshot via an fs path at runtime,
  // which Next's dependency tracing can't infer — so it's force-included into
  // each function bundle (pipeline/ is not deployed to Vercel).
  //
  // TWO path forms on purpose: Vercel sets the file-tracing ROOT to the repo
  // root (/vercel/path0) even though the app lives in web/, so an include
  // relative to that root must be "web/data/titans.db"; locally the root is the
  // web/ dir, so it's "data/titans.db". Listing both means whichever root is in
  // effect, the file is found and traced (the non-matching glob is a harmless
  // no-op). The runtime resolver (lib/db.ts) searches both layouts to match.
  //
  // Build runs with webpack (`next build --webpack`): Next 16 defaults `build`
  // to Turbopack, which does NOT honor these includes at all.
  outputFileTracingIncludes: {
    "/": ["./data/titans.db", "./web/data/titans.db"],
    "/api/chat": ["./data/titans.db", "./web/data/titans.db"],
    "/person/[slug]": ["./data/titans.db", "./web/data/titans.db"],
    "/company/[slug]": ["./data/titans.db", "./web/data/titans.db"],
  },
};

export default nextConfig;
