"""Tests for the gold-candidate finder — the 'what to verify next' helper.

Builds an in-memory DB with a handful of people/claims and asserts the filter
(needs current role + LinkedIn + coherent, excludes existing gold) and the
paste-ready snippet shape.
"""
from __future__ import annotations

import json
import sqlite3

from db import init_schema
from enrichment_store import init_enrichment_schema
from gold_candidates import find_candidates, render_candidates
from person_insights_store import init_person_insights_schema


def _conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    init_enrichment_schema(conn)
    init_person_insights_schema(conn)
    return conn


def _person(conn, pid, name):
    conn.execute(
        "INSERT INTO people (id, full_name, name_slug, titan_class, school, "
        "initial_company, city, source_url, raw_entry) VALUES "
        "(?,?,?,2,'Texas A&M','JP Morgan','Houston','https://r','raw')",
        (pid, name, f"slug-{pid}"),
    )


def _claim(conn, pid, ct, value, url="", method="firecrawl"):
    conn.execute(
        "INSERT INTO claims (person_id, claim_type, value, source_url, quote, "
        "confidence, extraction_method) VALUES (?,?,?,?,'',0.9,?)",
        (pid, ct, value, url, method),
    )


def _complete_person(conn, pid, name, *, method="firecrawl+pdl+reconciled"):
    _person(conn, pid, name)
    _claim(conn, pid, "current_employer", "Acme Capital", method=method)
    _claim(conn, pid, "current_title", "Partner")
    _claim(conn, pid, "education", "BBA From Texas A&M University (2010-2014)")
    _claim(conn, pid, "career_history", "Partner at Acme Capital (2018-present)")
    _claim(conn, pid, "public_links", "LinkedIn",
           url="https://linkedin.com/in/" + name.lower().replace(" ", "-"))


def test_finds_a_complete_coherent_person():
    conn = _conn()
    _complete_person(conn, 1, "Jane Doe")
    cands = find_candidates(conn, exclude_ids=set())
    assert len(cands) == 1
    c = cands[0]
    assert c.person_id == 1 and c.current_employer == "Acme Capital"
    assert "linkedin.com/in/jane-doe" in c.linkedin_url and c.corroborated


def test_excludes_gold_ids():
    conn = _conn()
    _complete_person(conn, 1, "Jane Doe")
    assert find_candidates(conn, exclude_ids={1}) == []


def test_skips_person_without_linkedin():
    conn = _conn()
    _person(conn, 2, "No Linkedin")
    _claim(conn, 2, "current_employer", "Acme")
    _claim(conn, 2, "current_title", "VP")
    assert find_candidates(conn, exclude_ids=set()) == []


def test_skips_person_without_current_role():
    conn = _conn()
    _person(conn, 3, "No Role")
    _claim(conn, 3, "public_links", "LinkedIn",
           url="https://linkedin.com/in/no-role")
    assert find_candidates(conn, exclude_ids=set()) == []


def test_skips_incoherent_person():
    conn = _conn()
    _person(conn, 4, "Future Person")
    _claim(conn, 4, "current_employer", "Acme")
    _claim(conn, 4, "current_title", "VP")
    _claim(conn, 4, "career_history", "VP at Acme (2030-present)")  # future date
    _claim(conn, 4, "public_links", "LinkedIn",
           url="https://linkedin.com/in/future")
    assert find_candidates(conn, exclude_ids=set()) == []


def test_require_corroborated_filters_single_source():
    conn = _conn()
    _complete_person(conn, 5, "Single Source", method="firecrawl")  # not reconciled
    assert find_candidates(conn, exclude_ids=set()) != []  # shows by default
    assert find_candidates(conn, exclude_ids=set(), require_corroborated=True) == []


def test_snippet_is_valid_gold_record():
    conn = _conn()
    _complete_person(conn, 1, "Jane Doe")
    cands = find_candidates(conn, exclude_ids=set())
    out = render_candidates(cands)
    assert "verify:" in out and "Jane Doe" in out
    # The paste-ready block must start with a parseable gold record.
    rec, _ = json.JSONDecoder().raw_decode(out[out.index("{"):])
    assert rec["source"] == "human-verified"
    assert rec["expect"]["current_employer"] == "Acme Capital"


def test_render_empty_is_friendly():
    assert "already in the gold set" in render_candidates([])
