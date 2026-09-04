import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import Database from "better-sqlite3";
import { afterAll, describe, expect, it } from "vitest";

// db.ts opens its file once per process from TITANS_DB_PATH, so the fixture is
// wired up BEFORE the module loads: a scratch copy of the committed synthetic
// sample.db with a namesake (same slug, later class) added, so the
// disambiguation path has something to bite on.
const FIXTURE = path.join(__dirname, "..", "data", "sample.db");
const scratch = path.join(
  os.tmpdir(),
  `titans_db_test_${Date.now()}_${Math.random().toString(36).slice(2)}.db`
);
fs.copyFileSync(FIXTURE, scratch);

interface SeedRow {
  id: number;
  full_name: string;
  name_slug: string;
  titan_class: number;
  school: string;
}

const seed = (() => {
  const w = new Database(scratch);
  try {
    const first = w
      .prepare("SELECT id, full_name, name_slug, titan_class, school FROM people ORDER BY id LIMIT 1")
      .get() as SeedRow;
    const namesakeClass = first.titan_class + 7;
    w.prepare(
      `INSERT INTO people (full_name, name_slug, titan_class, school, initial_company, city, source_url, needs_review, raw_entry)
       VALUES (?, ?, ?, ?, 'Namesake Capital', 'Austin', 'https://example.test/dir', 0, 'seeded namesake')`
    ).run(first.full_name, first.name_slug, namesakeClass, first.school);
    const other = w
      .prepare("SELECT id, full_name, name_slug, titan_class, school FROM people WHERE name_slug <> ? ORDER BY id LIMIT 1")
      .get(first.name_slug) as SeedRow;
    const distinctSources = (
      w.prepare("SELECT COUNT(DISTINCT source_url) AS n FROM claims WHERE source_url <> ''").get() as { n: number }
    ).n;
    return { first, namesakeClass, other, distinctSources };
  } finally {
    w.close();
  }
})();

process.env.TITANS_DB_PATH = scratch;
const db = await import("./db");

afterAll(() => {
  fs.rmSync(scratch, { force: true });
});

describe("getPersonBySlug namesakes", () => {
  it("keeps the plain slug URL resolving to the earliest class", () => {
    const p = db.getPersonBySlug(seed.first.name_slug);
    expect(p?.titan_class).toBe(seed.first.titan_class);
  });

  it("reaches the later-class namesake when the class is given", () => {
    const p = db.getPersonBySlug(seed.first.name_slug, seed.namesakeClass);
    expect(p?.titan_class).toBe(seed.namesakeClass);
    expect(p?.full_name).toBe(seed.first.full_name);
  });

  it("returns undefined for a class that has no such person", () => {
    expect(db.getPersonBySlug(seed.first.name_slug, 9999)).toBeUndefined();
  });

  it("ignores a stale class on a slug that has no namesake", () => {
    const p = db.getPersonBySlug(seed.other.name_slug, 9999);
    expect(p?.titan_class).toBe(seed.other.titan_class);
  });
});

describe("personHref", () => {
  it("emits the plain path for a unique slug", () => {
    expect(db.personHref(seed.other.name_slug, seed.other.titan_class)).toBe(
      `/person/${seed.other.name_slug}`
    );
  });

  it("emits the ?c= form for a slug shared by namesakes", () => {
    expect(db.personHref(seed.first.name_slug, seed.namesakeClass)).toBe(
      `/person/${seed.first.name_slug}?c=${seed.namesakeClass}`
    );
    expect(db.personHref(seed.first.name_slug, seed.first.titan_class)).toBe(
      `/person/${seed.first.name_slug}?c=${seed.first.titan_class}`
    );
  });

  it("is what member rows carry as their link", () => {
    for (const m of [...db.landingSectorMembers(), ...db.firstJobSectorMembers()]) {
      expect(m.href).toBe(db.personHref(m.slug, m.titanClass));
    }
  });
});

describe("directoryStats on a display DB", () => {
  it("counts distinct claim sources (person_sources ships empty)", () => {
    const stats = db.directoryStats();
    expect(stats.sources).toBe(seed.distinctSources);
    expect(stats.sources).toBeGreaterThan(0);
  });

  it("reports no review queue (null) when identity_candidates is empty", () => {
    expect(db.directoryStats().reviewQueue).toBeNull();
  });

  it("counts the review queue once identity_candidates has rows", () => {
    const w = new Database(scratch);
    try {
      const insert = w.prepare(
        `INSERT INTO identity_candidates (person_id, source_url, confidence, decision, model)
         VALUES (?, ?, 0.5, ?, 'test')`
      );
      insert.run(seed.first.id, "https://example.test/a", "review");
      insert.run(seed.first.id, "https://example.test/b", "accept");
    } finally {
      w.close();
    }
    expect(db.directoryStats().reviewQueue).toBe(1);
  });
});
