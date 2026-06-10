// Build semantic-search vectors for every alumnus, stored in the SOURCE-OF-TRUTH
// pipeline DB so `sync-db` copies them into the web snapshot (the web DB is
// overwritten on every dev/build, so vectors must live upstream).
//
// Local + free: a small sentence-transformer (all-MiniLM-L6-v2, 384-dim) runs
// in-process via transformers.js — no API key, no vendor, no per-call cost. Re-run
// after each enrichment batch:  node scripts/embed-people.mjs
//
// Vectors are stored as Float32 BLOBs in person_vectors(person_id, dim, vec, model).
import path from "node:path";
import { fileURLToPath } from "node:url";
import Database from "better-sqlite3";
import { pipeline } from "@xenova/transformers";

const MODEL = "Xenova/all-MiniLM-L6-v2";
const BATCH = 32;

const here = path.dirname(fileURLToPath(import.meta.url));
const DEFAULT_DB = path.resolve(here, "../../pipeline/data/titans.db");
const dbPath = process.argv[2] || DEFAULT_DB;

// One readable profile string per person — the signal a semantic query matches on.
// News is excluded (unverified namesake risk), mirroring the chat's grounding.
function profileText(row) {
  const parts = [
    row.full_name,
    row.school && `${row.school} Titans ${row.titan_class}`,
    row.city && row.city !== "(unknown)" && row.city,
    row.initial_company && `first employer: ${row.initial_company}`,
    row.current_sector,
    row.current_industry,
    row.job_function,
    row.pdl_seniority,
    row.claims_text,
  ];
  return parts.filter(Boolean).join(". ").replace(/\s+/g, " ").trim();
}

function loadPeople(db) {
  return db
    .prepare(
      `SELECT p.id, p.full_name, p.school, p.titan_class, p.initial_company, p.city,
              pi.current_sector, pi.current_industry, pi.job_function, pi.pdl_seniority,
              (SELECT group_concat(c.claim_type || ': ' || c.value, '. ')
                 FROM claims c
                WHERE c.person_id = p.id AND c.claim_type <> 'news_mention') AS claims_text
         FROM people p
         LEFT JOIN person_insights pi ON pi.person_id = p.id
         ORDER BY p.id`
    )
    .all();
}

async function main() {
  const db = new Database(dbPath);
  db.pragma("journal_mode = WAL");
  db.exec(
    `CREATE TABLE IF NOT EXISTS person_vectors (
       person_id INTEGER PRIMARY KEY,
       dim       INTEGER NOT NULL,
       vec       BLOB    NOT NULL,
       model     TEXT    NOT NULL,
       built_at  TEXT    NOT NULL DEFAULT (datetime('now'))
     )`
  );

  const people = loadPeople(db);
  const texts = people.map(profileText);
  console.log(`embedding ${people.length} people with ${MODEL} …`);

  const extractor = await pipeline("feature-extraction", MODEL);
  const upsert = db.prepare(
    `INSERT INTO person_vectors (person_id, dim, vec, model)
     VALUES (@person_id, @dim, @vec, @model)
     ON CONFLICT(person_id) DO UPDATE SET
       dim = excluded.dim, vec = excluded.vec, model = excluded.model,
       built_at = datetime('now')`
  );

  let done = 0;
  for (let i = 0; i < people.length; i += BATCH) {
    const slice = texts.slice(i, i + BATCH);
    const out = await extractor(slice, { pooling: "mean", normalize: true });
    const dim = out.dims[1];
    const writeBatch = db.transaction((rows) => {
      for (const r of rows) upsert.run(r);
    });
    const rows = slice.map((_, j) => {
      const vec = out.data.slice(j * dim, (j + 1) * dim);
      return {
        person_id: people[i + j].id,
        dim,
        vec: Buffer.from(new Float32Array(vec).buffer),
        model: MODEL,
      };
    });
    writeBatch(rows);
    done += rows.length;
    if (done % 256 === 0 || done === people.length) {
      console.log(`  ${done}/${people.length}`);
    }
  }
  db.close();
  console.log(`done — ${done} vectors (dim ${384}) written to ${dbPath}`);
}

main().catch((err) => {
  console.error("embed-people failed:", err);
  process.exit(1);
});
