"""Unit tests for pdl_verify's pure logic (partition / parse / keep-bias).

The live Haiku call is exercised by the re-verify backfill; here we lock down the
parsing and the two safety guarantees: non-gated claims pass through untouched,
and a missing/garbled verdict defaults to KEEP (the gate can only remove on an
explicit, parseable 'drop')."""
from __future__ import annotations

from enrichment_store import ClaimRow
from pdl_verify import _GATED_TYPES, _build_user, _parse, verify_pdl_claims


def _claim(ct, value):
    return ClaimRow(claim_type=ct, value=value, source_url="https://pdl",
                    quote="", confidence=0.9, extraction_method="pdl")


def test_gated_types_are_career_and_education_only():
    assert _GATED_TYPES == {"career_history", "education"}


def test_parse_keep_and_drop():
    text = '[{"index":0,"decision":"keep","reason":"finance"},{"index":1,"decision":"drop","reason":"pastor"}]'
    v = _parse(text, 2)
    assert v[0].keep is True and v[1].keep is False


def test_parse_missing_index_defaults_keep():
    # only index 0 returned; index 1 must default to keep
    v = _parse('[{"index":0,"decision":"drop","reason":"x"}]', 2)
    assert v[0].keep is False and v[1].keep is True


def test_parse_garbage_keeps_all():
    v = _parse("not json", 3)
    assert all(x.keep for x in v) and len(v) == 3


def test_parse_handles_fences():
    v = _parse('```json\n[{"index":0,"decision":"drop","reason":"r"}]\n```', 1)
    assert v[0].keep is False


def test_build_user_numbers_gated_entries():
    entries = [_claim("career_history", "Analyst at TRS"), _claim("education", "MBA from A&M")]
    user = _build_user("Jane Doe", "TRS", "Austin", entries)
    assert "[0] career_history: Analyst at TRS" in user
    assert "[1] education: MBA from A&M" in user
    assert "Jane Doe" in user


class _FakeResp:
    class _U:
        input_tokens = 10
        output_tokens = 5
    def __init__(self, text):
        self.usage = self._U()
        self.content = [type("B", (), {"type": "text", "text": text})()]


class _FakeClient:
    def __init__(self, text):
        self._text = text
        self.messages = self
    def create(self, **_):
        return _FakeResp(self._text)


def test_verify_drops_only_flagged_gated_claim_keeps_passthrough():
    claims = [
        _claim("current_employer", "TRS"),          # passthrough, never judged
        _claim("public_links", "LinkedIn"),          # passthrough
        _claim("career_history", "Analyst at TRS"),  # gated -> keep
        _claim("education", "Divinity degree, Ohio"),# gated -> drop
    ]
    client = _FakeClient('[{"index":0,"decision":"keep"},{"index":1,"decision":"drop"}]')
    kept, ti, to = verify_pdl_claims(client, "Jane Doe", "TRS", "Austin", claims)
    values = [c.value for c in kept]
    assert "TRS" in values and "LinkedIn" in values          # passthrough survived
    assert "Analyst at TRS" in values                        # kept gated
    assert "Divinity degree, Ohio" not in values             # dropped gated
    assert (ti, to) == (10, 5)


def test_verify_no_gated_claims_skips_call():
    claims = [_claim("current_employer", "TRS"), _claim("public_links", "LinkedIn")]
    # client.create would raise if called (no gated entries -> must short-circuit)
    kept, ti, to = verify_pdl_claims(None, "Jane Doe", "TRS", "Austin", claims)
    assert kept == claims and (ti, to) == (0, 0)
