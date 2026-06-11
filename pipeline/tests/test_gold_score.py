"""Pure-fixture tests for the gold scorer — the scorecard's answer key.

No DB, no LLM, no file I/O (GoldRecords are built in-process). Covers a clean
match, a wrong-date career miss, a namesake-leak hard fail, and a ghost-fill
hard fail. Mirrors test_coherence / test_scorecard factory style.
"""
from __future__ import annotations

import json

from enrichment_store import ClaimRow
from gold_score import (
    GoldRecord,
    load_gold,
    score_batch,
    score_person,
)


def _claim(ct, value, url="", quote=""):
    return ClaimRow(ct, value, url, quote, 0.9, "firecrawl")


def _positive(person_id=1):
    return GoldRecord(
        person_id=person_id, full_name="Jane Doe",
        expect={
            "current_employer": "Acme Capital",
            "current_title": "Partner",
            "education": ["Texas A&M"],
            "career": [{"company": "Acme Capital", "start": 2018, "end": None}],
            "linkedin_url": "https://www.linkedin.com/in/jane-doe",
        },
        must_reject_urls=(), must_stay_empty=False,
    )


def _clean_claims():
    return [
        _claim("current_employer", "Acme Capital, Inc."),  # suffix-tolerant
        _claim("current_title", "Partner"),
        _claim("education", "BBA from Texas A&M University"),
        _claim("career_history", "Partner at Acme Capital (2018-present)"),
        _claim("public_links", "Jane Doe | LinkedIn",
               url="https://www.linkedin.com/in/jane-doe"),
    ]


# --- accuracy ------------------------------------------------------------------

def test_clean_match_scores_100():
    res = score_person(_positive(), _clean_claims())
    assert res.accuracy == 100 and not res.violations


def test_wrong_career_dates_dock_accuracy():
    claims = _clean_claims()
    # Replace the career entry with a wrong-decade start (2008 not 2018).
    claims = [c for c in claims if c.claim_type != "career_history"]
    claims.append(_claim("career_history", "Partner at Acme Capital (2008-present)"))
    res = score_person(_positive(), claims)
    assert res.fields["career"] == 0.0
    assert res.accuracy < 100  # one of five fields missed


def test_missing_employer_docks_accuracy():
    claims = [c for c in _clean_claims() if c.claim_type != "current_employer"]
    res = score_person(_positive(), claims)
    assert res.fields["employer"] == 0.0


# --- identity hard fails -------------------------------------------------------

def _ghost(person_id=2, must_reject=()):
    return GoldRecord(person_id=person_id, full_name="Ghosty McGhost",
                      expect={}, must_reject_urls=tuple(must_reject),
                      must_stay_empty=True)


def test_ghost_stays_empty_passes():
    res = score_person(_ghost(), [_claim("public_links", "maybe", url="https://x")])
    assert res.accuracy is None and not res.violations


def test_ghost_filled_trips_violation():
    res = score_person(_ghost(), [_claim("current_employer", "Imposter LLC")])
    assert res.violations and "must_stay_empty" in res.violations[0]


def test_must_reject_url_leak_trips_violation():
    rec = _ghost(must_reject=("wwana.com",))
    claims = [_claim("public_links", "broker echo",
                     url="https://www.wwana.com/ricardo-lopez")]
    res = score_person(rec, claims)
    assert res.violations and "must-reject" in res.violations[0]


# --- batch aggregation + gate --------------------------------------------------

def test_batch_clean_is_not_gated():
    rep = score_batch([_positive(1), _ghost(2)],
                      {1: _clean_claims(), 2: [_claim("public_links", "x")]})
    assert rep.accuracy == 100 and rep.identity_score == 100
    assert rep.gold_n == 2 and rep.positives == 1 and not rep.gated


def test_batch_with_leak_is_gated():
    rep = score_batch([_ghost(2, must_reject=("wwana.com",))],
                      {2: [_claim("public_links", "x", url="http://wwana.com/p")]})
    assert rep.gated and rep.identity_score == 0


def test_batch_ignores_gold_not_in_scope():
    # Only person 1 is in the batch; the ghost (2) isn't loaded.
    rep = score_batch([_positive(1), _ghost(2)], {1: _clean_claims()})
    assert rep.gold_n == 1 and not rep.gated


# --- loader validation ---------------------------------------------------------

def test_load_gold_round_trip(tmp_path):
    path = tmp_path / "gold.json"
    path.write_text(json.dumps([
        {"person_id": 5, "full_name": "A", "must_stay_empty": True},
    ]))
    recs = load_gold(path)
    assert len(recs) == 1 and recs[0].person_id == 5 and recs[0].must_stay_empty


def test_load_gold_missing_file_is_empty(tmp_path):
    assert load_gold(tmp_path / "nope.json") == []


def test_load_gold_rejects_malformed(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps([{"full_name": "no id"}]))
    try:
        load_gold(path)
        assert False, "should have raised"
    except ValueError:
        pass
