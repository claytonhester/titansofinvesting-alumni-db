"""Unit tests for the fail-closed LinkedIn profile verifier."""
from __future__ import annotations

import json

from enrichment_store import ClaimRow
from linkedin_verify import (
    DECISION_REJECTED,
    DECISION_REVIEW,
    DECISION_VERIFIED,
    _build_user,
    _parse_verdict,
    verify_linkedin_profile,
)


class _FakeResp:
    class _U:
        input_tokens = 10
        output_tokens = 5

    def __init__(self, text):
        self.usage = self._U()
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _FakeClient:
    def __init__(self, text=None, exc=None):
        self._text = text
        self._exc = exc
        self.messages = self

    def create(self, **_):
        if self._exc:
            raise self._exc
        return _FakeResp(self._text)


def _claims():
    return [
        ClaimRow("education", "BBA in Finance from Texas A&M University",
                 "https://linkedin.com/in/x", "", 0.8, "firecrawl-linkedin"),
        ClaimRow("career_history", "Analyst at JP Morgan (2007-2010)",
                 "https://linkedin.com/in/x", "", 0.8, "firecrawl-linkedin"),
    ]


def _verify(client, claims=None):
    return verify_linkedin_profile(
        client, "Jane Doe",
        profile_url="https://linkedin.com/in/x",
        school="Texas A&M", grad_year=2007,
        roster_employer="JP Morgan", city="Houston",
        claims=_claims() if claims is None else claims,
    )


# --- verdict outcomes ---------------------------------------------------------

def test_verified_verdict_parsed():
    text = json.dumps({"decision": "verified", "reason": "era + employer match", "confidence": 0.92})
    verdict, tin, tout = _verify(_FakeClient(text))
    assert verdict.verified and verdict.decision == DECISION_VERIFIED
    assert verdict.confidence == 0.92
    assert (tin, tout) == (10, 5)


def test_rejected_and_review_verdicts():
    rej, _, _ = _verify(_FakeClient(json.dumps({"decision": "rejected", "reason": "wrong era", "confidence": 0.9})))
    rev, _, _ = _verify(_FakeClient(json.dumps({"decision": "review", "reason": "no early career shown", "confidence": 0.4})))
    assert rej.decision == DECISION_REJECTED and not rej.verified
    assert rev.decision == DECISION_REVIEW and not rev.verified


# --- fail-closed posture ------------------------------------------------------

def test_malformed_json_rejects():
    verdict, _, _ = _verify(_FakeClient("I think it's probably them."))
    assert verdict.decision == DECISION_REJECTED


def test_unknown_decision_rejects():
    verdict, _, _ = _verify(_FakeClient(json.dumps({"decision": "maybe", "confidence": 0.9})))
    assert verdict.decision == DECISION_REJECTED


def test_api_error_rejects_with_zero_tokens():
    verdict, tin, tout = _verify(_FakeClient(exc=RuntimeError("boom")))
    assert verdict.decision == DECISION_REJECTED
    assert (tin, tout) == (0, 0)


def test_empty_claims_reject_without_model_call():
    verdict, tin, tout = _verify(_FakeClient(exc=AssertionError("must not call")), claims=[])
    assert verdict.decision == DECISION_REJECTED
    assert (tin, tout) == (0, 0)


def test_confidence_clamped():
    verdict, _, _ = _verify(_FakeClient(json.dumps({"decision": "verified", "confidence": 7})))
    assert verdict.confidence == 1.0


def test_markdown_fenced_json_parsed():
    fenced = "```json\n" + json.dumps({"decision": "verified", "reason": "ok", "confidence": 0.8}) + "\n```"
    assert _parse_verdict(fenced).verified


# --- prompt content -----------------------------------------------------------

def test_user_prompt_carries_all_anchors():
    user = _build_user("Jane Doe", "https://linkedin.com/in/x", "Texas A&M",
                       2007, "JP Morgan", "Houston", _claims())
    for anchor in ("Jane Doe", "Texas A&M", "2007", "JP Morgan", "Houston",
                   "linkedin.com/in/x", "BBA in Finance"):
        assert anchor in user
