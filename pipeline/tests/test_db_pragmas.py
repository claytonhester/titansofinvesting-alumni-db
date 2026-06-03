import sqlite3
from pathlib import Path

import pytest

import db as db_module


@pytest.mark.unit
def test_connect_enables_wal_and_busy_timeout(tmp_path: Path) -> None:
    db_path = tmp_path / "pragma_check.db"
    with db_module.connect(db_path) as conn:
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        busy_timeout = conn.execute("PRAGMA busy_timeout").fetchone()[0]

    assert journal_mode.lower() == "wal"
    assert busy_timeout >= 5000
