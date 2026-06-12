// Copy the pipeline's read-only SQLite snapshot INTO web/data/ so it ships with
// the Next deployment. The web app reads ./data/titans.db (see lib/db.ts); on
// Vercel the pipeline/ dir is not in the build context, so this committed
// snapshot is the only copy that exists at runtime. Refreshing alumni data is
// therefore: re-run enrichment -> `npm run sync-db` -> commit -> redeploy.
//
// Tolerant by design:
//   - source present  -> copy it (normal local/pre-deploy path)
//   - source missing but dest present -> keep the committed snapshot (Vercel
//     build, where only web/ is available) and exit 0
//   - both missing -> fail loudly so a broken deploy can't ship an empty app
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";

const here = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.join(here, "..");
const source = path.join(webRoot, "..", "pipeline", "data", "titans.db");
const destDir = path.join(webRoot, "data");
const dest = path.join(destDir, "titans.db");

function log(msg) {
  process.stdout.write(`[sync-db] ${msg}\n`);
}

// The pipeline writes the DB in WAL mode, which CANNOT be opened read-only on a
// read-only filesystem (Vercel): SQLite must create the -shm/-wal sidecars and
// fails with "unable to open database file". Fold any pending WAL frames into
// the main file and switch the shipped snapshot to a plain rollback journal so
// the serverless runtime can open it read-only with no sidecars at all.
function makeReadOnlySafe(dbPath) {
  // Clear any sidecars first: a -shm copied from a live writer carries lock
  // state and makes the checkpoint fail SQLITE_BUSY. The pipeline checkpoints
  // before handing off, so the main file already holds all committed data.
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
  // Drop now-orphaned sidecars so they can't ship or confuse the reader.
  for (const ext of ["-wal", "-shm"]) {
    const sidecar = dbPath + ext;
    if (fs.existsSync(sidecar)) fs.rmSync(sidecar);
  }
  const mode = new Database(dbPath, { readonly: true }).pragma("journal_mode", {
    simple: true,
  });
  log(`snapshot journal_mode=${mode} (read-only-safe)`);
}

if (fs.existsSync(source)) {
  fs.mkdirSync(destDir, { recursive: true });
  fs.copyFileSync(source, dest);
  const kb = Math.round(fs.statSync(dest).size / 1024);
  log(`copied pipeline snapshot -> web/data/titans.db (${kb} KB)`);
  makeReadOnlySafe(dest);
} else if (fs.existsSync(dest)) {
  log("pipeline source not in build context — using committed web/data/titans.db");
  makeReadOnlySafe(dest);
} else {
  process.stderr.write(
    "[sync-db] FATAL: no titans.db at pipeline/data/ or web/data/. " +
      "Run the pipeline first, or commit a snapshot to web/data/titans.db.\n"
  );
  process.exit(1);
}
