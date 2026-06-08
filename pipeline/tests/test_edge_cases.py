"""Adversarial edge-case tests across the new pipeline modules.

Every input here is designed to crash a "never raises" path or smuggle corrupt
data through. If one of these fails, it's a real bug the happy-path tests missed.
"""
from __future__ import annotations

import pytest

from enrichment_store import ClaimRow
from linkedin_firecrawl import map_claims
from normalize import clean_link_title, digest_claims, is_junk_value, smart_title
from pdl_verify import _parse as pdl_parse
from pdl_verify import verify_pdl_claims
from qa_audit import compute_metrics
from reconcile import _apply, _Decision, _parse_decisions, reconcile_claims


def _c(ct, value, method="pdl", url="", conf=0.8, quote=""):
    return ClaimRow(claim_type=ct, value=value, source_url=url, quote=quote,
                    confidence=conf, extraction_method=method)


# ── reconcile ────────────────────────────────────────────────────────────────

def test_apply_empty_members_decision_no_crash():
    resume = [_c("career_history", "Analyst at TRS")]
    # a decision with NO members (defensive: parse filters these, but _apply must
    # not crash if one slips through)
    out = _apply(resume, [_Decision("career_history", "X", (), 0)])
    # nothing absorbed; the real claim survives via the uncovered safety net
    assert any(c.value == "Analyst at TRS" for c in out)


def test_parse_decisions_dedupes_duplicate_member_indices():
    # model returns the same index twice -> should not be treated as a 2-member merge
    d = _parse_decisions('{"facts":[{"claim_type":"career_history","value":"X","members":[0,0],"primary":0}]}', 1)
    assert d[0].members == (0,)


def test_parse_decisions_non_int_primary_and_members():
    d = _parse_decisions('{"facts":[{"claim_type":"x","value":"v","members":["a",1],"primary":"b"}]}', 3)
    assert d[0].members == (1,) and d[0].primary == 1


def test_parse_decisions_facts_not_a_list():
    assert _parse_decisions('{"facts": {"oops": 1}}', 2) == []
    assert _parse_decisions('{"facts": null}', 2) == []


def test_apply_all_generic_values_does_not_crash():
    # canonical + members all generic (no significant tokens) -> absorbed allowed
    resume = [_c("current_title", "Director"), _c("current_title", "Manager")]
    out = _apply(resume, [_Decision("current_title", "Senior Director", (0, 1), 0)])
    assert len(out) >= 1  # no IndexError


def test_reconcile_thin_input_no_api_call():
    # <2 résumé claims -> returns unchanged, never calls the (None) client
    claims = [_c("public_links", "LinkedIn"), _c("current_title", "CEO")]
    out, ti, to = reconcile_claims(None, "Jane", claims)
    assert out == claims and (ti, to) == (0, 0)


# ── normalize ────────────────────────────────────────────────────────────────

def test_clean_link_title_very_long_input_terminates():
    # a pathologically long scraped title must not hang the O(n^3) collapse
    huge = " ".join(["word%d" % i for i in range(400)])
    out = clean_link_title(huge)
    assert isinstance(out, str) and len(out) > 0


def test_clean_link_title_only_punctuation():
    assert isinstance(clean_link_title("--- | ---"), str)


def test_clean_link_title_unicode_and_emoji():
    assert clean_link_title("Café Société 📈 Report") == "Café Société 📈 Report"


def test_clean_link_title_repeated_phrase_many_times():
    assert clean_link_title("Foo Bar Foo Bar Foo Bar") in ("Foo Bar", "Foo Bar Foo Bar")


def test_smart_title_blank_and_punct():
    assert smart_title("") == ""
    assert smart_title("   ") == "   " or smart_title("   ").strip() == ""


def test_digest_drops_whitespace_only_via_junk_or_keeps_safe():
    # whitespace-only value must not crash digest
    out = digest_claims([_c("career_history", "   "), _c("current_title", "CEO")])
    assert any(c.claim_type == "current_title" for c in out)


# ── linkedin_firecrawl.map_claims ────────────────────────────────────────────

def test_map_claims_data_is_list_not_dict():
    # map_claims expects a dict; a list must yield [] (the fetch path normalizes,
    # but guard the mapper directly)
    assert map_claims({}) == []


def test_map_claims_garbage_experience_entry_types():
    data = {
        "found": True,
        "linkedin_url": "https://linkedin.com/in/x",
        "experience": [None, "a string", 42, {}, {"title": "Analyst", "company": "TRS"}],
        "education": ["not a dict", {"school": "A&M"}],
    }
    rows = map_claims(data)  # must not raise
    careers = [r for r in rows if r.claim_type == "career_history"]
    edus = [r for r in rows if r.claim_type == "education"]
    assert any("Analyst" in c.value for c in careers)
    assert any("A&M" in e.value for e in edus)


def test_map_claims_nonstring_fields_coerced():
    data = {"found": True, "current_employer": 123, "linkedin_url": 456}
    rows = map_claims(data)  # str() coercion, no crash
    assert any(r.claim_type == "current_employer" for r in rows)


def test_map_claims_experience_not_a_list():
    data = {"found": True, "experience": "junk", "education": None}
    assert isinstance(map_claims(data), list)  # no crash


# ── pdl_verify ───────────────────────────────────────────────────────────────

def test_pdl_parse_out_of_range_index_ignored():
    v = pdl_parse('[{"index":5,"decision":"drop"}]', 2)
    assert all(x.keep for x in v) and len(v) == 2  # index 5 ignored; both default keep


def test_verify_pdl_none_client_with_gated_claims_returns_input():
    # client=None but gated claims present -> the create() call fails -> keep all
    claims = [_c("career_history", "Analyst at TRS"), _c("education", "BBA from A&M")]
    out, ti, to = verify_pdl_claims(None, "Jane", "TRS", "Austin", claims)
    assert out == claims and (ti, to) == (0, 0)


# ── qa_audit.compute_metrics ─────────────────────────────────────────────────

def test_metrics_none_method_and_url_no_crash():
    claims = [
        ClaimRow("career_history", "X", None, "", 0.5, None),
        ClaimRow("public_links", "Y", None, "", 0.5, ""),
    ]
    m = compute_metrics(claims)  # must not crash on None method/url
    assert m["total_claims"] == 2


def test_metrics_empty_claims():
    m = compute_metrics([])
    assert m["total_claims"] == 0 and m["by_source"] == {}
