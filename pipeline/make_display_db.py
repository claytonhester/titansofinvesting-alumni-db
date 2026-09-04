"""Build a DISPLAY-ONLY copy of the research DB for public hosting.

The live website is meant to be public — it serves the directory, insights, and
person/company pages to the world. But the full research database also holds
behind-the-scenes tables whose ROWS should not leave the machine: raw
identity-match candidates (namesake URLs the gate rejected), source-discovery
logs, pipeline run status, and geocode caches.

This script produces a copy that empties those internal tables, so the file we
host (on Vercel Blob, which the server reads via TITANS_DB_URL) exposes nothing
beyond what's already public on the site. It is deterministic and safe to re-run
on every data refresh.

What the web app does with the emptied tables (keep this accurate — an earlier
version of this docstring claimed they were "not rendered anywhere", which was
false: the Build Status page counted person_sources and identity_candidates):
  * person_sources     — the web no longer reads it; the per-person "sources"
                         count is derived from the claims table instead.
  * identity_candidates — the web reads it only for the "to review" stat, and
                         hides that stat when the table is empty (display DB).
  * batch_status, geocode_cache — never read by the web.

Usage:
    python make_display_db.py [SOURCE_DB] [OUTPUT_DB]
    # defaults: data/titans.db -> data/titans_display.db
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

# Internal research scaffolding, emptied for hosting (see the module docstring
# for how the web copes with each one being empty). Emptied, not dropped, so the
# schema stays identical to the real DB for any code that introspects it.
INTERNAL_TABLES = (
    "identity_candidates",
    "person_sources",
    "batch_status",
    "geocode_cache",
)


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def make_display_db(source: Path, output: Path) -> dict[str, int]:
    """Copy `source` to `output`, empty the internal tables, and leave the file
    read-only-safe (rollback journal, no sidecars). Returns rows-cleared counts."""
    if not source.exists():
        raise FileNotFoundError(f"source DB not found: {source}")

    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, output)
    # Drop any copied sidecars so the conversion below can't hit a stale lock.
    for ext in ("-wal", "-shm"):
        sidecar = output.with_name(output.name + ext)
        if sidecar.exists():
            sidecar.unlink()

    cleared: dict[str, int] = {}
    conn = sqlite3.connect(output)
    try:
        conn.execute("PRAGMA busy_timeout = 5000")
        for table in INTERNAL_TABLES:
            if _table_exists(conn, table):
                before = conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                conn.execute(f"DELETE FROM {table}")
                cleared[table] = before
        conn.commit()
        # Reclaim space and switch to a rollback journal so the hosted file opens
        # read-only on a read-only filesystem (matches web/scripts/sync-db.mjs).
        conn.execute("PRAGMA journal_mode = DELETE")
        conn.execute("VACUUM")
    finally:
        conn.close()

    for ext in ("-wal", "-shm"):
        sidecar = output.with_name(output.name + ext)
        if sidecar.exists():
            sidecar.unlink()
    return cleared


def main() -> None:
    here = Path(__file__).resolve().parent
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "data" / "titans.db"
    output = (
        Path(sys.argv[2]) if len(sys.argv) > 2 else here / "data" / "titans_display.db"
    )
    cleared = make_display_db(source, output)

    conn = sqlite3.connect(f"file:{output}?mode=ro", uri=True)
    try:
        people = conn.execute("SELECT count(*) FROM people").fetchone()[0]
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()

    print(f"Display DB written: {output}")
    print(f"  people: {people}  journal_mode: {mode}")
    print("  internal tables emptied:")
    for table, n in cleared.items():
        print(f"    {table}: {n} rows removed")


if __name__ == "__main__":
    main()
