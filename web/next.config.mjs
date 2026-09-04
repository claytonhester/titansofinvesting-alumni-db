const isDev = process.env.NODE_ENV !== "production";

// Content-Security-Policy. The app loads nothing third-party: no external
// scripts, fonts, images, or fetches — everything is same-origin.
//   script-src: Next's hydration / RSC flight payload is inline <script>, so
//     'unsafe-inline' is required without a per-request nonce (which needs
//     middleware and costs static optimisation). Dev adds 'unsafe-eval' for
//     React Refresh only.
//   style-src: 'unsafe-inline' for the style={{ width }} meter/bar fills in
//     page.tsx, insights-views.tsx, and kpi-modal.tsx.
const csp = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDev ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data:",
  "font-src 'self'",
  "connect-src 'self'",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const securityHeaders = [
  { key: "Content-Security-Policy", value: csp },
  { key: "X-Content-Type-Options", value: "nosniff" },
  { key: "X-Frame-Options", value: "DENY" },
  { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
];

/** @type {import('next').NextConfig} */
const nextConfig = {
  async headers() {
    return [{ source: "/:path*", headers: securityHeaders }];
  },
  // Heavy module kept external to the server bundle: @xenova/transformers
  // (native addon) and the in-process embedding model (onnxruntime backend).
  serverExternalPackages: ["@xenova/transformers"],
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
  //
  // sample.db is traced too: an open-source deploy with no real DB ships the
  // synthetic snapshot, and lib/db.ts falls back to it.
  outputFileTracingIncludes: {
    "/": ["./data/*.db", "./web/data/*.db"],
    "/api/chat": ["./data/*.db", "./web/data/*.db"],
    "/person/[slug]": ["./data/*.db", "./web/data/*.db"],
    "/company/[slug]": ["./data/*.db", "./web/data/*.db"],
  },
};

export default nextConfig;
