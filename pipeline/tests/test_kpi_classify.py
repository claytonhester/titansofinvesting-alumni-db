"""Unit tests for kpi_classify: deterministic fallback, parse, client merge."""
from __future__ import annotations

from enrichment_store import ClaimRow
from kpi_classify import (
    KpiFlags,
    classify_kpis,
    deterministic_flags,
    _parse,
)


def _c(ct, value, quote=""):
    return ClaimRow(ct, value, "", quote, 0.8, "pdl")


def test_deterministic_buy_side_and_md():
    claims = [
        _c("current_title", "Managing Director"),
        _c("current_employer", "Blackstone Capital Partners"),
        _c("career_history", "Analyst at Goldman (2010-2014)"),
    ]
    f = deterministic_flags(claims, first_employer="Goldman")
    assert f.on_buy_side is True
    assert f.reached_md is True
    assert f.still_first_firm is False


def test_deterministic_founder_partner():
    claims = [
        _c("current_title", "Founder & CEO"),
        _c("current_employer", "Veritas Fund"),
    ]
    f = deterministic_flags(claims, first_employer="JP Morgan")
    assert f.founder_partner is True


def test_deterministic_still_first_firm_normalized():
    claims = [
        _c("current_title", "Partner"),
        _c("current_employer", "Bain & Co"),
    ]
    f = deterministic_flags(claims, first_employer="bain  &  co")
    assert f.still_first_firm is True


def test_deterministic_sell_side_not_buy_side():
    claims = [
        _c("current_title", "Senior Consultant"),
        _c("current_employer", "Deloitte Consulting"),
    ]
    f = deterministic_flags(claims, first_employer="Deloitte Consulting")
    assert f.on_buy_side is False
    assert f.still_first_firm is True


def test_deterministic_analyst_not_md():
    claims = [_c("current_title", "Investment Analyst"),
              _c("current_employer", "TRS")]
    f = deterministic_flags(claims, first_employer="TRS")
    assert f.reached_md is False


def test_parse_fenced_json():
    obj = _parse('```json\n{"on_buy_side": true, "reached_md": false, '
                 '"founder_partner": false, "still_first_firm": true}\n```')
    assert obj["on_buy_side"] is True and obj["still_first_firm"] is True


def test_parse_garbage_returns_none():
    assert _parse("not json") is None


from types import SimpleNamespace


class _Resp:
    def __init__(self, text):
        self.content = [SimpleNamespace(type="text", text=text)]
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5)


def _make_client(text=None, exc=None):
    def create(**_):
        if exc:
            raise exc
        return _Resp(text)

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_classify_no_client_uses_fallback():
    claims = [_c("current_title", "Managing Director"), _c("current_employer", "KKR")]
    flags, ti, to = classify_kpis(None, "Jane", 2010, "KKR", claims)
    assert isinstance(flags, KpiFlags) and ti == 0 and to == 0
    assert flags.reached_md is True


def test_classify_client_success_overrides_fallback():
    claims = [_c("current_title", "Analyst"), _c("current_employer", "TRS")]
    client = _make_client(
        '{"on_buy_side": true, "reached_md": true, '
        '"founder_partner": false, "still_first_firm": false}'
    )
    flags, ti, to = classify_kpis(client, "Jane", 2010, "TRS", claims)
    assert flags.reached_md is True  # model overrode the deterministic False
    assert ti == 10 and to == 5


def test_classify_client_error_falls_back():
    claims = [_c("current_title", "Founder"), _c("current_employer", "Veritas")]
    client = _make_client(exc=RuntimeError("boom"))
    flags, ti, to = classify_kpis(client, "Jane", 2010, "X", claims)
    assert flags.founder_partner is True and ti == 0 and to == 0


def test_classify_partial_json_fills_from_fallback():
    claims = [_c("current_title", "Managing Director"), _c("current_employer", "KKR")]
    client = _make_client('{"on_buy_side": true}')  # missing 3 keys
    flags, _, _ = classify_kpis(client, "Jane", 2010, "KKR", claims)
    assert flags.on_buy_side is True
    assert flags.reached_md is True  # filled from deterministic fallback
