"""Pure parsing of the Titans directory HTML into PersonRecords.

Kept free of I/O so it can be unit-tested against fixtures. The directory is a
Squarespace page: cohort headings are <p><strong>Titans N</strong></p> and each
person is a <p>Name, Company, City</p>. Company may itself contain commas, so the
rule is: first comma-field = name, last = city, middle (rejoined) = company.
"""
from __future__ import annotations

import re

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


def _parse_entry(
    text: str, titan_class: int, school: str, source_url: str
) -> PersonRecord | None:
    parts = [p.strip() for p in text.split(",")]
    parts = [p for p in parts if p]
    if len(parts) < 2:
        return None  # not a person row (stray text, header, etc.)

    name = parts[0]
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

    return records
