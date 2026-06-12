// Prepare the read-only SQLite DB the Next app reads (see lib/db.ts). On Vercel
// the pipeline/ dir is NOT in the build context, so this script decides which DB
// the deployment ships, in priority order:
//
//   1. TITANS_DB_URL set       -> download the REAL DB from a private URL into
//      web/data/titans.db. This is how a production deploy of a PUBLIC repo gets
//      real data without the data ever living in the repo.
//   2. pipeline/data/titans.db -> copy it (normal local/pre-deploy path with the
//      real data on your machine).
//   3. web/data/titans.db present -> use the committed real snapshot as-is
//      (legacy / private-repo deploy).
//   4. none of the above       -> fall back to the committed SYNTHETIC
//      web/data/sample.db. The app (lib/db.ts) reads titans.db if present, else
//      sample.db, so an open-source clone "just works" with fake data.
//
// Every DB we hand off is made read-only-safe (rollback journal, no sidecars).
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.join(here, "..");
const source = path.join(webRoot, "..", "pipeline", "data", "titans.db");
const destDir = path.join(webRoot, "data");
const dest = path.join(destDir, "titans.db");
const sample = path.join(destDir, "sample.db");
const dbUrl = process.env.TITANS_DB_URL;

function log(msg) {
  process.stdout.write(`[sync-db] ${msg}\n`);
}

// SQLite in WAL mode CANNOT be opened read-only on a read-only filesystem
// (Vercel): it must create -shm/-wal sidecars and fails with "unable to open
// database file". Switch the shipped DB to a plain rollback journal so the
// serverless runtime opens it read-only with no sidecars at all.
function makeReadOnlySafe(dbPath) {
  // Clear sidecars first: a -shm copied from a live writer carries lock state
  // and makes the conversion fail SQLITE_BUSY.
  for (const ext of ["-wal", "-shm"]) {
    const sidecar = dbPath + ext;
    if (fs.existsSync(sidecar)) fs.rmSync(sidecar);
  }
  const conn = new Database(dbPath);
  try {
    conn.pragma("busy_timeout = 5000");
    conn.pragma("journal_mode = DELETE");
  } finally {
    conn.close();
  }
  for (const ext of ["-wal", "-shm"]) {
    const sidecar = dbPath + ext;
    if (fs.existsSync(sidecar)) fs.rmSync(sidecar);
  }
  const mode = new Database(dbPath, { readonly: true }).pragma("journal_mode", {
    simple: true,
  });
  log(`${path.basename(dbPath)} journal_mode=${mode} (read-only-safe)`);
}

async function downloadTo(url, outPath) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`TITANS_DB_URL fetch failed: ${res.status} ${res.statusText}`);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  fs.writeFileSync(outPath, buf);
  return buf.length;
}

fs.mkdirSync(destDir, { recursive: true });

if (dbUrl) {
  const bytes = await downloadTo(dbUrl, dest);
  log(`downloaded real DB from TITANS_DB_URL -> web/data/titans.db (${Math.round(bytes / 1024)} KB)`);
  makeReadOnlySafe(dest);
} else if (fs.existsSync(source)) {
  fs.copyFileSync(source, dest);
  log(`copied pipeline snapshot -> web/data/titans.db (${Math.round(fs.statSync(dest).size / 1024)} KB)`);
  makeReadOnlySafe(dest);
} else if (fs.existsSync(dest)) {
  log("pipeline source not in build context — using committed web/data/titans.db");
  makeReadOnlySafe(dest);
} else if (fs.existsSync(sample)) {
  log("no real DB available — app will use the synthetic web/data/sample.db");
  makeReadOnlySafe(sample);
} else {
  process.stderr.write(
    "[sync-db] FATAL: no real titans.db (TITANS_DB_URL / pipeline / web) and no " +
      "web/data/sample.db. Run `python pipeline/make_sample_db.py` to generate the sample.\n"
  );
  process.exit(1);
}
