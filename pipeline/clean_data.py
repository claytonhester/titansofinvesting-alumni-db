"""One-off data hygiene pass over data/titans.db.

Phase-1 ingest left a handful of blemishes in otherwise clean rows, and the
`initial_company` column mixes real firm names with role prefixes
("Entrepreneur, X", "Graduate program, X"). Research (Phase 2) anchors on the
company, so this script also derives a clean `research_company` for every
alumnus.

Safe by construction: backs up the DB first, runs every change in a single
transaction, and prints a before/after audit. Re-runnable (idempotent).

    python clean_data.py            # apply
    python clean_data.py --dry-run  # report only, write nothing
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from pathlib import Path

from models import slugify

DB_PATH = Path(__file__).parent / "data" / "titans.db"
BACKUP_PATH = DB_PATH.with_suffix(".db.bak")

# --- explicit row fixes (id -> correction) -------------------------------
# Malformed names spotted in the directory dump.
NAME_FIXES: dict[int, str] = {
    340: "Haylee (Whitehead) Burke",   # was "Haylee (Whitehead( Burke"
    479: "Britain Winchell",           # was "Britain Winchell Winchell"
}
# Junk city placeholders -> blanked + flagged for manual review.
JUNK_CITIES = {"(unknown)", "unknown", "moved", "n/a", "na", ""}
# Slug collision: two distinct Devan Patels (classes 12 & 15) share a slug,
# which breaks the web /person/[slug] route. Disambiguate the later one.
SLUG_OVERRIDES: dict[int, str] = {1048: "devan-patel-2"}

_ROLE_PREFIXES = ("Entrepreneur,", "Graduate program,")
_ROLE_ONLY = {"Entrepreneur", "Graduate program"}


def research_company(initial_company: str) -> str:
    """The clean entity to anchor Phase-2 research on. Strips role prefixes;
    bare role-only values yield '' (no usable company anchor)."""
    s = initial_company.strip()
    for prefix in _ROLE_PREFIXES:
        if s.startswith(prefix):
            return s[len(prefix):].strip()
    if s in _ROLE_ONLY:
        return ""
    return s


def ensure_column(conn: sqlite3.Connection) -> None:
    cols = {r[1] for r in conn.execute("PRAGMA table_info(people)")}
    if "research_company" not in cols:
        conn.execute("ALTER TABLE people ADD COLUMN research_company TEXT NOT NULL DEFAULT ''")


def run(dry_run: bool) -> int:
    if not DB_PATH.exists():
        print(f"error: {DB_PATH} not found", file=sys.stderr)
        return 1

    if not dry_run:
        shutil.copy2(DB_PATH, BACKUP_PATH)
        print(f"backup written: {BACKUP_PATH}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        ensure_column(conn)
        rows = conn.execute(
            "SELECT id, full_name, name_slug, initial_company, city, "
            "needs_review, research_company FROM people"
        ).fetchall()

        name_changes, slug_changes, city_changes, anchor_changes = [], [], [], []
        updates: list[tuple] = []

        for r in rows:
            rid = r["id"]
            full_name = NAME_FIXES.get(rid, r["full_name"])
            if full_name != r["full_name"]:
                name_changes.append((rid, r["full_name"], full_name))

            # Slug: explicit override wins, else recompute from (possibly fixed) name.
            new_slug = SLUG_OVERRIDES.get(rid, slugify(full_name))
            if new_slug != r["name_slug"]:
                slug_changes.append((rid, r["name_slug"], new_slug))

            city, needs_review = r["city"], r["needs_review"]
            if (city or "").strip().lower() in JUNK_CITIES:
                city_changes.append((rid, r["city"]))
                city, needs_review = "", 1

            anchor = research_company(r["initial_company"])
            if anchor != (r["research_company"] or ""):
                anchor_changes.append((rid, r["initial_company"], anchor))

            updates.append((full_name, new_slug, city, needs_review, anchor, rid))

        print(f"\nrows scanned:           {len(rows)}")
        print(f"name fixes:             {len(name_changes)}")
        for rid, old, new in name_changes:
            print(f"   #{rid}: {old!r} -> {new!r}")
        print(f"slug fixes:             {len(slug_changes)}")
        for rid, old, new in slug_changes:
            print(f"   #{rid}: {old!r} -> {new!r}")
        print(f"junk cities blanked:    {len(city_changes)}")
        for rid, old in city_changes:
            print(f"   #{rid}: {old!r} -> '' (needs_review=1)")
        print(f"research_company set:    {len(anchor_changes)}")
        missing = sum(1 for _, _, a in anchor_changes if a == "")
        print(f"   (of which empty anchor / role-only: {missing})")

        if dry_run:
            print("\ndry-run: no changes written.")
            return 0

        conn.executemany(
            "UPDATE people SET full_name=?, name_slug=?, city=?, "
            "needs_review=?, research_company=?, updated_at=datetime('now') "
            "WHERE id=?",
            updates,
        )
        conn.commit()
        # Verify slug uniqueness held after the rewrite.
        dupes = conn.execute(
            "SELECT name_slug, count(*) c FROM people GROUP BY name_slug HAVING c>1"
        ).fetchall()
        if dupes:
            print("\nWARNING: duplicate slugs remain:", [d["name_slug"] for d in dupes])
        else:
            print("\nslug uniqueness: OK")
        print("done.")
        return 0
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="Clean data/titans.db in place.")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    args = ap.parse_args()
    return run(args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
