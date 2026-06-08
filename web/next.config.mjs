/** @type {import('next').NextConfig} */
const nextConfig = {
  // better-sqlite3 is a native module; keep it external to the server bundle.
  serverExternalPackages: ["better-sqlite3"],
  // Pin the workspace root so Turbopack ignores lockfiles further up the tree.
  turbopack: { root: import.meta.dirname },
  // The chat route reads ./data/titans.db via an fs path at runtime, which Next's
  // dependency tracing can't infer. Force the snapshot into the function bundle
  // so it exists on Vercel (where pipeline/ is not deployed).
  outputFileTracingIncludes: {
    "/api/chat": ["./data/titans.db"],
    "/person/[slug]": ["./data/titans.db"],
    "/": ["./data/titans.db"],
  },
};

export default nextConfig;
