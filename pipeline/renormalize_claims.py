"""One-time / repeatable re-normalization of stored claim values.

Existing claims were written before the casing rules in ``normalize.py`` were
hardened (acronyms like LLC/KKR, '&' handling such as "A&m" -> "A&M"). This
re-applies ``smart_title`` to every title-cased claim type so the DB matches the
current rules. It is idempotent — values already clean are left untouched — and
only rewrites ``value`` (the verbatim ``quote`` is never modified).

Usage:
    python renormalize_claims.py            # apply to pipeline/data/titans.db
    python renormalize_claims.py --dry-run  # report what would change, no write
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from normalize import _TITLE_CASE_TYPES, is_junk_value, smart_title

_DEFAULT_DB = Path(__file__).resolve().parent / "data" / "titans.db"


def renormalize(db_path: Path, *, dry_run: bool = False) -> list[tuple[int, str, str]]:
    """Re-title-case eligible claim values in ``db_path``.

    Returns the list of (claim_id, old_value, new_value) that changed (or would
    change, under --dry-run).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        placeholders = ",".join("?" for _ in _TITLE_CASE_TYPES)
        rows = conn.execute(
            f"SELECT id, value FROM claims WHERE claim_type IN ({placeholders})",
            tuple(_TITLE_CASE_TYPES),
        ).fetchall()

        changes = [
            (claim_id, value, smart_title(value))
            for claim_id, value in rows
            if smart_title(value) != value
        ]

        if changes and not dry_run:
            conn.executemany(
                "UPDATE claims SET value = ? WHERE id = ?",
                [(new, claim_id) for claim_id, _, new in changes],
            )
            conn.commit()
        return changes
    finally:
        conn.close()


def purge_junk(db_path: Path, *, dry_run: bool = False) -> list[tuple[int, str, str]]:
    """Delete claims whose value is a placeholder/boolean (e.g. "True").

    Returns the list of (claim_id, claim_type, value) removed (or that would be
    removed, under --dry-run).
    """
    if not db_path.exists():
        raise FileNotFoundError(f"DB not found: {db_path}")

    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT id, claim_type, value FROM claims").fetchall()
        junk = [
            (claim_id, claim_type, value)
            for claim_id, claim_type, value in rows
            if is_junk_value(value)
        ]
        if junk and not dry_run:
            conn.executemany(
                "DELETE FROM claims WHERE id = ?",
                [(claim_id,) for claim_id, _, _ in junk],
            )
            conn.commit()
        return junk
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=_DEFAULT_DB)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    changes = renormalize(args.db, dry_run=args.dry_run)
    verb = "Would update" if args.dry_run else "Updated"
    print(f"{verb} {len(changes)} claim value(s) in {args.db}")
    for claim_id, old, new in changes:
        print(f"  [{claim_id}] {old!r} -> {new!r}")

    junk = purge_junk(args.db, dry_run=args.dry_run)
    verb = "Would remove" if args.dry_run else "Removed"
    print(f"{verb} {len(junk)} junk claim(s) in {args.db}")
    for claim_id, claim_type, value in junk:
        print(f"  [{claim_id}] {claim_type}={value!r}")


if __name__ == "__main__":
    main()
