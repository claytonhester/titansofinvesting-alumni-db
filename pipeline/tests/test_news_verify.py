"""Unit tests for news_verify's pure logic (verdict parsing)."""
from news_verify import Verdict, _parse_verdicts


class TestParseVerdicts:
    def test_parses_clean_array(self):
        text = '[{"index":0,"verdict":"yes","reason":"FINRA record"},' \
               '{"index":1,"verdict":"no","reason":"therapist"}]'
        v = _parse_verdicts(text, 2)
        assert [x.verdict for x in v] == ["yes", "no"]
        assert v[0].is_match is True
        assert v[1].is_match is False

    def test_strips_code_fences(self):
        text = '```json\n[{"index":0,"verdict":"yes","reason":"ok"}]\n```'
        v = _parse_verdicts(text, 1)
        assert v[0].verdict == "yes"

    def test_missing_indices_become_unsure(self):
        text = '[{"index":0,"verdict":"yes","reason":"ok"}]'
        v = _parse_verdicts(text, 3)
        assert len(v) == 3
        assert v[0].verdict == "yes"
        assert v[1].verdict == "unsure"
        assert v[2].verdict == "unsure"

    def test_unknown_verdict_value_coerced_to_unsure(self):
        text = '[{"index":0,"verdict":"maybe","reason":"x"}]'
        assert _parse_verdicts(text, 1)[0].verdict == "unsure"

    def test_malformed_json_all_unsure(self):
        v = _parse_verdicts("not json at all", 2)
        assert len(v) == 2
        assert all(x.verdict == "unsure" for x in v)

    def test_embedded_array_recovered(self):
        text = 'Here are the verdicts: [{"index":0,"verdict":"no","reason":"general"}] done'
        assert _parse_verdicts(text, 1)[0].verdict == "no"

    def test_is_match_only_true_for_yes(self):
        assert Verdict(0, "yes", "").is_match is True
        assert Verdict(0, "no", "").is_match is False
        assert Verdict(0, "unsure", "").is_match is False
