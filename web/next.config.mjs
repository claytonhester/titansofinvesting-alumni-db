/** @type {import('next').NextConfig} */
const nextConfig = {
  // better-sqlite3 is a native module; keep it external to the server bundle.
  serverExternalPackages: ["better-sqlite3"],
  // Pin the workspace root so Turbopack ignores lockfiles further up the tree.
  turbopack: { root: import.meta.dirname },
};

export default nextConfig;
