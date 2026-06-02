"""Export the cleaned directory to flat, agent-friendly files.

The canonical store is data/titans.db, but an enrichment agent runs best over a
stream. This writes:

    data/titans_clean.jsonl  one alumnus per line (stream this in a loop)
    data/titans_clean.csv    same rows, for humans / spreadsheets

Each record carries `research_company` — the clean firm/institution to anchor
research on (role prefixes already stripped). Re-run after any clean_data pass.

    python export_list.py
"""
from __future__ import annotations

import csv
import json
import sqlite3
from pathlib import Path

DATA = Path(__file__).parent / "data"
DB_PATH = DATA / "titans.db"
JSONL_PATH = DATA / "titans_clean.jsonl"
CSV_PATH = DATA / "titans_clean.csv"

FIELDS = (
    "id",
    "full_name",
    "name_slug",
    "titan_class",
    "school",
    "initial_company",
    "research_company",
    "city",
    "needs_review",
    "source_url",
)


def main() -> int:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT {', '.join(FIELDS)} FROM people ORDER BY titan_class, full_name"
        ).fetchall()
    finally:
        conn.close()

    with JSONL_PATH.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: r[k] for k in FIELDS}, ensure_ascii=False) + "\n")

    with CSV_PATH.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r[k] for k in FIELDS})

    with_company = sum(1 for r in rows if (r["research_company"] or "").strip())
    print(f"exported {len(rows)} alumni")
    print(f"   with research_company anchor: {with_company}")
    print(f"   no company (name+city only):  {len(rows) - with_company}")
    print(f"   {JSONL_PATH}")
    print(f"   {CSV_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
