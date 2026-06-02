"""Phase 1: ingest the public Titans directory into SQLite.

fetch → snapshot raw HTML → parse → upsert. Every run saves a timestamped
snapshot so the parse is reproducible and we have an audit trail of what the
public page looked like on a given date. Public data only; no auth, no keys.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx

from config import DIRECTORY_URL, SNAPSHOT_DIR, USER_AGENT
from db import connect, count_people, init_schema, upsert_people
from parser import parse_directory


@dataclass(frozen=True)
class IngestResult:
    parsed: int
    needs_review: int
    snapshot_path: Path
    total_in_db: int


def fetch_html(url: str = DIRECTORY_URL, timeout: float = 30.0) -> str:
    headers = {"User-Agent": USER_AGENT}
    response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.text


def save_snapshot(html: str, snapshot_dir: Path = SNAPSHOT_DIR) -> Path:
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = snapshot_dir / f"directory_{stamp}.html"
    path.write_text(html, encoding="utf-8")
    return path


def ingest(url: str = DIRECTORY_URL, html: str | None = None) -> IngestResult:
    if html is None:
        html = fetch_html(url)
    snapshot_path = save_snapshot(html)

    records = parse_directory(html, source_url=url)
    needs_review = sum(1 for r in records if r.needs_review)

    with connect() as conn:
        init_schema(conn)
        upsert_people(conn, records)
        total = count_people(conn)

    return IngestResult(
        parsed=len(records),
        needs_review=needs_review,
        snapshot_path=snapshot_path,
        total_in_db=total,
    )
