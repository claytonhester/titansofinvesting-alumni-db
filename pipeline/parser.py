"""Pure parsing of the Titans directory HTML into PersonRecords.

Kept free of I/O so it can be unit-tested against fixtures. The directory is a
Squarespace page: cohort headings are <p><strong>Titans N</strong></p> and each
person is a <p>Name, Company, City</p>. Company may itself contain commas, so the
rule is: first comma-field = name, last = city, middle (rejoined) = company.
"""
from __future__ import annotations

import re
from collections import defaultdict

from bs4 import BeautifulSoup

from models import PersonRecord, slugify

# Matches "Titans 7", "Baylor University Titans 3", "University of Texas Titans 15".
_HEADING_RE = re.compile(
    r"^(?:(University of Texas|Baylor University|Baylor)\s+)?Titans\s+(\d+)$",
    re.IGNORECASE,
)
_DEFAULT_SCHOOL = "Texas A&M"
_UNKNOWN_CITY = "(unknown)"


def _school_from_prefix(prefix: str | None) -> str:
    if not prefix:
        return _DEFAULT_SCHOOL
    return "University of Texas" if prefix.lower().startswith("university") else "Baylor"


def _parse_heading(text: str) -> tuple[int, str] | None:
    match = _HEADING_RE.match(text.strip())
    if not match:
        return None
    return int(match.group(2)), _school_from_prefix(match.group(1))


def _dedupe_trailing_word(name: str) -> str:
    """Collapse an immediately-repeated trailing word: 'Britain Winchell
    Winchell' -> 'Britain Winchell'. This is a recurring typo in the public
    directory; collapsing it keeps the parse stable and idempotent. Only the
    exact final-word duplication is touched — internal repeats are left alone."""
    words = name.split()
    if len(words) >= 2 and words[-1].lower() == words[-2].lower():
        return " ".join(words[:-1])
    return name


def _parse_entry(
    text: str, titan_class: int, school: str, source_url: str
) -> PersonRecord | None:
    parts = [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None  # not a person row (stray text, header, etc.)

    name = _dedupe_trailing_word(parts[0])
    if len(parts) == 2:
        company, city, needs_review = parts[1], _UNKNOWN_CITY, True
    else:
        company, city, needs_review = ", ".join(parts[1:-1]), parts[-1], False

    return PersonRecord(
        full_name=name,
        name_slug=slugify(name),
        titan_class=titan_class,
        school=school,
        initial_company=company,
        city=city,
        source_url=source_url,
        needs_review=needs_review,
        raw_entry=text.strip(),
    )


def _assign_unique_slugs(records: list[PersonRecord]) -> list[PersonRecord]:
    """Make every slug globally unique so /person/<slug> resolves one person.

    Two alumni can share a name (e.g. two 'Devan Patel's in different cohorts).
    The DB key is (name_slug, titan_class, school), but the web URL is the slug
    alone — so same-name people MUST get distinct slugs or one is unreachable.

    Disambiguation is DETERMINISTIC and STABLE: within a colliding base slug,
    records are ordered by (titan_class, school, full_name); the first keeps the
    base slug and the rest get '-2', '-3', … Because the order key never changes,
    re-parsing the same directory always yields the same slugs, so a re-ingest is
    a true no-op instead of spawning duplicate rows."""
    by_base: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        by_base[r.name_slug].append(i)

    out = list(records)
    for base, idxs in by_base.items():
        if len(idxs) == 1:
            continue
        ordered = sorted(
            idxs,
            key=lambda i: (records[i].titan_class, records[i].school, records[i].full_name),
        )
        for rank, i in enumerate(ordered):
            if rank == 0:
                continue  # first occurrence keeps the bare base slug
            out[i] = records[i].model_copy(update={"name_slug": f"{base}-{rank + 1}"})
    return out


def parse_directory(html: str, source_url: str) -> list[PersonRecord]:
    """Walk every <p> inside content blocks in document order, tracking the
    current cohort/school from headings and emitting a record per person row."""
    soup = BeautifulSoup(html, "html.parser")
    records: list[PersonRecord] = []
    titan_class: int | None = None
    school = _DEFAULT_SCHOOL

    for block in soup.select("div.sqs-html-content"):
        for p in block.find_all("p"):
            text = p.get_text().replace("\xa0", " ").strip()
            if not text:
                continue

            heading = _parse_heading(text)
            if heading is not None:
                titan_class, school = heading
                continue

            if titan_class is None:
                continue  # text before the first cohort heading — skip

            record = _parse_entry(text, titan_class, school, source_url)
            if record is not None:
                records.append(record)

    return _assign_unique_slugs(records)
