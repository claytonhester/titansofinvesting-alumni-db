"""Unit tests for the pure directory parser.

The fixture mirrors the real Squarespace DOM: text blocks are
<div class="sqs-html-content"> wrappers, cohort headings are
<p><strong>Titans N</strong></p>, person rows are <p>Name, Company, City</p>,
and &nbsp;-only paragraphs act as spacers.
"""
from __future__ import annotations

import pytest

from parser import parse_directory

SOURCE_URL = "https://example.test/directory"

FIXTURE_HTML = """
<html><body>
  <div class="sqs-html-content" data-sqsp-text-block-content>
    <p>Some intro text before any cohort heading.</p>
    <p><strong>Titans 0</strong></p>
    <p>Cason Beckham, Teacher Retirement System of Texas, Austin</p>
    <p>&nbsp;</p>
    <p>Jason Kaspar, Veritas Ark Fund, Texas Precious Metals, Austin</p>
    <p><strong>Baylor University Titans 2</strong></p>
    <p>Jane Doe, Acme Capital, Waco</p>
    <p>Solo Name Only, BigCo</p>
  </div>
  <div class="sqs-html-content" data-sqsp-text-block-content>
    <p><strong>University of Texas Titans 15</strong></p>
    <p>John Roe, Longhorn Ventures, Dallas</p>
  </div>
</body></html>
"""


@pytest.fixture
def records():
    return parse_directory(FIXTURE_HTML, SOURCE_URL)


@pytest.mark.unit
def test_skips_text_before_first_heading(records):
    assert all(r.raw_entry != "Some intro text before any cohort heading." for r in records)


@pytest.mark.unit
def test_record_count(records):
    # 2 (Titans 0) + 2 (Baylor 2) + 1 (UT 15) = 5
    assert len(records) == 5


@pytest.mark.unit
def test_simple_entry_fields(records):
    cason = records[0]
    assert cason.full_name == "Cason Beckham"
    assert cason.initial_company == "Teacher Retirement System of Texas"
    assert cason.city == "Austin"
    assert cason.titan_class == 0
    assert cason.school == "Texas A&M"
    assert cason.needs_review is False
    assert cason.name_slug == "cason-beckham"


@pytest.mark.unit
def test_company_with_internal_commas(records):
    kaspar = records[1]
    assert kaspar.full_name == "Jason Kaspar"
    assert kaspar.initial_company == "Veritas Ark Fund, Texas Precious Metals"
    assert kaspar.city == "Austin"
    assert kaspar.needs_review is False


@pytest.mark.unit
def test_baylor_heading_sets_school(records):
    jane = records[2]
    assert jane.school == "Baylor"
    assert jane.titan_class == 2


@pytest.mark.unit
def test_two_field_entry_flagged_for_review(records):
    solo = records[3]
    assert solo.full_name == "Solo Name Only"
    assert solo.initial_company == "BigCo"
    assert solo.city == "(unknown)"
    assert solo.needs_review is True


@pytest.mark.unit
def test_ut_heading_sets_school_and_class(records):
    john = records[4]
    assert john.school == "University of Texas"
    assert john.titan_class == 15
    assert john.city == "Dallas"


@pytest.mark.unit
def test_nbsp_spacers_produce_no_records(records):
    assert all(r.raw_entry.strip() not in ("", "\xa0") for r in records)


# ── Idempotency / data-quality fixes ─────────────────────────────────────────

_DUP_NAME_HTML = """
<html><body>
  <div class="sqs-html-content">
    <p><strong>University of Texas Titans 12</strong></p>
    <p>Devan Patel, Acme Capital, Austin</p>
    <p><strong>University of Texas Titans 15</strong></p>
    <p>Devan Patel, Other Fund, Austin</p>
    <p>Britain Winchell Winchell, Caterpillar, Houston</p>
  </div>
</body></html>
"""


@pytest.mark.unit
def test_same_name_gets_deterministic_unique_slugs():
    recs = parse_directory(_DUP_NAME_HTML, SOURCE_URL)
    devans = [r for r in recs if r.full_name == "Devan Patel"]
    assert len(devans) == 2
    by_class = {r.titan_class: r.name_slug for r in devans}
    # Ordered by titan_class: class 12 keeps the base slug, class 15 gets -2.
    assert by_class[12] == "devan-patel"
    assert by_class[15] == "devan-patel-2"


@pytest.mark.unit
def test_slug_assignment_is_stable_across_runs():
    a = parse_directory(_DUP_NAME_HTML, SOURCE_URL)
    b = parse_directory(_DUP_NAME_HTML, SOURCE_URL)
    assert [r.name_slug for r in a] == [r.name_slug for r in b]


@pytest.mark.unit
def test_doubled_trailing_surname_is_collapsed():
    recs = parse_directory(_DUP_NAME_HTML, SOURCE_URL)
    britain = [r for r in recs if r.full_name.startswith("Britain")]
    assert len(britain) == 1
    assert britain[0].full_name == "Britain Winchell"
    assert britain[0].name_slug == "britain-winchell"
