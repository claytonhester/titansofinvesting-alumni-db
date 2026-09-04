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
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { DatabaseSync } from "node:sqlite";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.join(here, "..");
const source = path.join(webRoot, "..", "pipeline", "data", "titans.db");
const destDir = path.join(webRoot, "data");
const dest = path.join(destDir, "titans.db");
const sample = path.join(destDir, "sample.db");
const dbUrl = process.env.TITANS_DB_URL;
// Optional hex SHA-256 of the file at TITANS_DB_URL. When set, a download that
// doesn't match is refused — so a swapped or truncated object can't ship.
const dbSha256 = process.env.TITANS_DB_SHA256;

function log(msg) {
  process.stdout.write(`[sync-db] ${msg}\n`);
}

// The private DB URL must be https: a plain-http fetch of the entire research
// database could be read or replaced in transit.
function requireHttps(url) {
  let parsed;
  try {
    parsed = new URL(url);
  } catch {
    throw new Error(`TITANS_DB_URL is not a valid URL: ${url}`);
  }
  if (parsed.protocol !== "https:") {
    throw new Error(`TITANS_DB_URL must use https: (got ${parsed.protocol})`);
  }
  return parsed;
}

function journalMode(dbPath) {
  const conn = new DatabaseSync(dbPath, { readOnly: true });
  try {
    return conn.prepare("PRAGMA journal_mode").get().journal_mode;
  } finally {
    conn.close();
  }
}

// SQLite in WAL mode CANNOT be opened read-only on a read-only filesystem
// (Vercel): it must create -shm/-wal sidecars and fails with "unable to open
// database file". Switch the shipped DB to a plain rollback journal so the
// serverless runtime opens it read-only with no sidecars at all.
//
// A file already in DELETE mode with no sidecars is left untouched — never
// opened for writing — so a committed, read-only-safe snapshot (sample.db) is
// not rewritten (and the working tree not dirtied) on every build.
function makeReadOnlySafe(dbPath) {
  const hasSidecar = ["-wal", "-shm"].some((ext) => fs.existsSync(dbPath + ext));
  if (!hasSidecar && journalMode(dbPath) === "delete") {
    log(`${path.basename(dbPath)} already journal_mode=delete (read-only-safe)`);
    return true;
  }
  return false;
}

function convertToRollbackJournal(dbPath) {
  // Clear sidecars first: a -shm copied from a live writer carries lock state
  // and makes the conversion fail SQLITE_BUSY.
  for (const ext of ["-wal", "-shm"]) {
    const sidecar = dbPath + ext;
    if (fs.existsSync(sidecar)) fs.rmSync(sidecar);
  }
  const conn = new DatabaseSync(dbPath);
  try {
    conn.exec("PRAGMA busy_timeout = 5000");
    conn.exec("PRAGMA journal_mode = DELETE");
  } finally {
    conn.close();
  }
  for (const ext of ["-wal", "-shm"]) {
    const sidecar = dbPath + ext;
    if (fs.existsSync(sidecar)) fs.rmSync(sidecar);
  }
  log(`${path.basename(dbPath)} journal_mode=${journalMode(dbPath)} (read-only-safe)`);
}

// Snapshots we own (downloaded / copied into web/data/titans.db) may be
// rewritten in place; a tracked file must not be.
function ensureReadOnlySafe(dbPath) {
  if (!makeReadOnlySafe(dbPath)) convertToRollbackJournal(dbPath);
}

async function downloadTo(url, outPath) {
  const res = await fetch(url);
  if (!res.ok) {
    throw new Error(`TITANS_DB_URL fetch failed: ${res.status} ${res.statusText}`);
  }
  const buf = Buffer.from(await res.arrayBuffer());
  if (dbSha256) {
    const actual = crypto.createHash("sha256").update(buf).digest("hex");
    if (actual !== dbSha256.trim().toLowerCase()) {
      throw new Error(
        `TITANS_DB_SHA256 mismatch: expected ${dbSha256.trim().toLowerCase()}, got ${actual} — refusing to ship the download`
      );
    }
    log(`sha256 verified (${actual.slice(0, 12)}…)`);
  }
  fs.writeFileSync(outPath, buf);
  return buf.length;
}

fs.mkdirSync(destDir, { recursive: true });

if (dbUrl) {
  requireHttps(dbUrl);
  const bytes = await downloadTo(dbUrl, dest);
  log(`downloaded real DB from TITANS_DB_URL -> web/data/titans.db (${Math.round(bytes / 1024)} KB)`);
  ensureReadOnlySafe(dest);
} else if (fs.existsSync(source)) {
  fs.copyFileSync(source, dest);
  log(`copied pipeline snapshot -> web/data/titans.db (${Math.round(fs.statSync(dest).size / 1024)} KB)`);
  ensureReadOnlySafe(dest);
} else if (fs.existsSync(dest)) {
  log("pipeline source not in build context — using committed web/data/titans.db");
  ensureReadOnlySafe(dest);
} else if (fs.existsSync(sample)) {
  log("no real DB available — app will use the synthetic web/data/sample.db");
  // sample.db is a TRACKED file: verify it is already read-only-safe rather
  // than rewriting it in place. Regenerate it if this ever fails.
  if (!makeReadOnlySafe(sample)) {
    process.stderr.write(
      "[sync-db] FATAL: web/data/sample.db is not in journal_mode=delete (or has " +
        "-wal/-shm sidecars). Regenerate it with `python pipeline/make_sample_db.py` " +
        "instead of rewriting the tracked file.\n"
    );
    process.exit(1);
  }
} else {
  process.stderr.write(
    "[sync-db] FATAL: no real titans.db (TITANS_DB_URL / pipeline / web) and no " +
      "web/data/sample.db. Run `python pipeline/make_sample_db.py` to generate the sample.\n"
  );
  process.exit(1);
}
