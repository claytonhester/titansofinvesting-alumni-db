"""Cross-language guard: the sector taxonomy must stay identical on both sides.

`sector_classify.py` (pipeline, writes person_insights.current_sector) and
`web/lib/db.ts` (search facet + sector cards) classify the SAME alumni into the
SAME buckets. If a keyword, an industry mapping, or a sector label drifts between
them, the snapshot and the live web views silently disagree. This test parses the
TS tables and asserts they mirror the python ones, byte-for-byte by content.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from sector_classify import (
    INDUSTRY_MAP,
    SECTOR_CATCHALL,
    SECTOR_NAMES,
    SECTOR_RULES,
)

_DB_TS = Path(__file__).resolve().parents[2] / "web" / "lib" / "db.ts"


def _block(text: str, marker: str) -> str:
    """Return the text of an array literal `const <marker> ... = [ ... ];`.

    Anchors on the `=` before the literal, so `[]` inside a `string[]` type
    annotation in the declaration doesn't get mistaken for the array start."""
    start = text.index(marker)
    eq = text.index("=", start)
    open_br = text.index("[", eq)
    depth = 0
    for i in range(open_br, len(text)):
        if text[i] == "[":
            depth += 1
        elif text[i] == "]":
            depth -= 1
            if depth == 0:
                return text[open_br : i + 1]
    raise AssertionError(f"unterminated array for {marker}")


def _parse_pairs(block: str, list_key: str) -> dict[str, tuple[str, ...]]:
    """Parse `{ sector: "X", <list_key>: ["a", "b"] }` objects into {sector: (...)}."""
    out: dict[str, tuple[str, ...]] = {}
    pattern = re.compile(
        r'sector:\s*"([^"]+)"[^}]*?' + list_key + r":\s*\[(.*?)\]",
        re.DOTALL,
    )
    for sector, body in pattern.findall(block):
        words = tuple(re.findall(r'"([^"]*)"', body))
        out[sector] = words
    return out


def _ts_tables() -> tuple[dict, dict, list[str], str]:
    text = _DB_TS.read_text(encoding="utf-8")
    industry = _parse_pairs(_block(text, "const INDUSTRY_MAP"), "needles")
    rules = _parse_pairs(_block(text, "const SECTOR_RULES"), "keywords")
    names_block = _block(text, "SECTOR_NAMES: readonly string[]")
    names = re.findall(r'"([^"]+)"', names_block)
    m = re.search(r'SECTOR_CATCHALL = "([^"]+)"', text)
    assert m, "SECTOR_CATCHALL not found in db.ts"
    return industry, rules, names, m.group(1)


@pytest.mark.unit
def test_catchall_matches() -> None:
    _, _, _, ts_catchall = _ts_tables()
    assert ts_catchall == SECTOR_CATCHALL


@pytest.mark.unit
def test_industry_map_matches() -> None:
    ts_industry, _, _, _ = _ts_tables()
    py_industry = {sector: tuple(needles) for sector, needles in INDUSTRY_MAP}
    assert ts_industry == py_industry


@pytest.mark.unit
def test_sector_rules_match() -> None:
    _, ts_rules, _, _ = _ts_tables()
    py_rules = {sector: tuple(kw) for sector, kw in SECTOR_RULES}
    assert ts_rules == py_rules


@pytest.mark.unit
def test_sector_names_match() -> None:
    text = _DB_TS.read_text(encoding="utf-8")
    names_block = _block(text, "SECTOR_NAMES: readonly string[]")
    quoted = re.findall(r'"([^"]+)"', names_block)
    # The TS list ends with the bare `SECTOR_CATCHALL` identifier (not a quoted
    # literal), so the named portion is the quoted entries and the catch-all is
    # appended via the constant. Reconstruct and compare to py SECTOR_NAMES.
    assert "SECTOR_CATCHALL" in names_block
    assert [*quoted, SECTOR_CATCHALL] == list(SECTOR_NAMES)
