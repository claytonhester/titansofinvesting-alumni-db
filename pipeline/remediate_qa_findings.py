"""One-off data remediation for the 2026-06-09 QA findings.

The CODE fixes (commit ea02588) prevent recurrence; this removes the rows that were
already written to the source-of-truth DB before the fixes landed:

  1. Ricardo Lopez (person 779) — a data-broker namesake (Boston University / Morgan
     Stanley / New York) wrongly merged onto a UT roster row. Cleared to honest-empty
     (roster row kept; claims/sources/insights/identity_candidates removed). A paid
     re-resolution would reach the SAME end state — his only sources are brokers, which
     the fixed prefilter now routes to Sonnet, which already rejects them.
  2. Two company false-matches in person_company:
       - Karn Nopany (97) "Lincoln Financial Group" -> lincolninternational.com
       - Brock Birkenfeld (90) "Shift Admin"          -> sageadvisory.com
  3. The TRS "Highest Paid State Employees / earned $408,000" news item (a public
     salary-records row shown as Recognition) — every news_curated row whose host is a
     public-records / salary database.

SAFETY: dry-run by default (prints what WOULD change, mutates nothing). Pass --apply
to back up the DB (timestamped .bak-pre-remediation) and run inside a single
transaction. Idempotent — safe to re-run.

    python remediate_qa_findings.py            # dry run
    python remediate_qa_findings.py --apply     # back up + apply
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from config import DB_PATH
from directory_hosts import PUBLIC_RECORDS_HOSTS

# News rows from these hosts are public-records disclosures, not editorial news.
_PUBLIC_RECORDS_HOSTS = tuple(sorted(PUBLIC_RECORDS_HOSTS))

_NAMESAKE_PERSON_ID = 779  # Ricardo Lopez
_NAMESAKE_TABLES = ("claims", "person_sources", "identity_candidates", "person_insights")

# (person_id, domain) person_company rows that are token-collision false matches.
_FALSE_MATCHES = (
    (97, "lincolninternational.com"),
    (90, "sageadvisory.com"),
)


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many rows each remediation step would remove, given current data."""
    out: dict[str, int] = {}
    for table in _NAMESAKE_TABLES:
        out[f"779:{table}"] = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE person_id = ?", (_NAMESAKE_PERSON_ID,)
        ).fetchone()[0]
    for pid, domain in _FALSE_MATCHES:
        out[f"person_company:{pid}->{domain}"] = conn.execute(
            "SELECT COUNT(*) FROM person_company WHERE person_id = ? AND domain = ?",
            (pid, domain),
        ).fetchone()[0]
    placeholders = ",".join("?" * len(_PUBLIC_RECORDS_HOSTS))
    out["news_curated:public-records"] = conn.execute(
        f"SELECT COUNT(*) FROM news_curated WHERE source_host IN ({placeholders})",
        _PUBLIC_RECORDS_HOSTS,
    ).fetchone()[0]
    return out


def _apply(conn: sqlite3.Connection) -> None:
    for table in _NAMESAKE_TABLES:
        conn.execute(
            f"DELETE FROM {table} WHERE person_id = ?", (_NAMESAKE_PERSON_ID,)
        )
    for pid, domain in _FALSE_MATCHES:
        conn.execute(
            "DELETE FROM person_company WHERE person_id = ? AND domain = ?",
            (pid, domain),
        )
    placeholders = ",".join("?" * len(_PUBLIC_RECORDS_HOSTS))
    conn.execute(
        f"DELETE FROM news_curated WHERE source_host IN ({placeholders})",
        _PUBLIC_RECORDS_HOSTS,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--apply", action="store_true",
        help="back up the DB and apply the deletes (default: dry run)",
    )
    parser.add_argument("--db", default=str(DB_PATH), help="path to titans.db")
    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    with sqlite3.connect(db_path) as conn:
        before = _counts(conn)
    total = sum(before.values())
    print(f"Remediation targets in {db_path}:")
    for key, n in before.items():
        print(f"  {n:>3}  {key}")
    print(f"  ---  {total} rows total")

    if not args.apply:
        print("\nDRY RUN — nothing changed. Re-run with --apply to back up + delete.")
        return 0

    if total == 0:
        print("\nNothing to remediate (already clean). No changes made.")
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = db_path.with_name(f"{db_path.name}.bak-pre-remediation-{stamp}")
    shutil.copy2(db_path, backup)
    print(f"\nBacked up -> {backup}")

    with sqlite3.connect(db_path) as conn:
        _apply(conn)
        conn.commit()
        after = _counts(conn)

    print("Applied. Post-remediation residual counts (expect all 0):")
    for key, n in after.items():
        print(f"  {n:>3}  {key}")
    print("\nNext: `npm run sync-db` from web/, then restart the dev server "
          "(better-sqlite3 caches the DB handle).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
