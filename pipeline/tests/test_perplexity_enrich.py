"""Unit tests for the Perplexity adapter's pure logic (query + parsing)."""
from perplexity_enrich import PerplexityResult, build_query, _parse


class TestBuildQuery:
    def test_name_only(self):
        assert build_query("Jane Roe") == '"Jane Roe"'

    def test_name_and_meaningful_employer(self):
        assert build_query("Jane Roe", "Longhorn Ventures") == '"Jane Roe" Longhorn Ventures'

    def test_generic_employer_falls_back(self):
        assert build_query("Jane Roe", "Capital Group") == '"Jane Roe"'

    def test_empty_name(self):
        assert build_query("  ") == ""


class TestParse:
    def test_parses_results(self):
        body = {"results": [
            {"title": "Leadership", "url": "http://x.com/a", "snippet": "CEO of Acme",
             "date": "2025-04-21", "last_updated": "2026-03-16"},
            {"title": "Profile", "url": "http://y.com/b", "snippet": ""},
        ]}
        rows = _parse(body)
        assert len(rows) == 2
        assert isinstance(rows[0], PerplexityResult)
        assert rows[0].title == "Leadership"
        assert rows[0].snippet == "CEO of Acme"
        assert rows[0].date == "2025-04-21"

    def test_falls_back_to_last_updated_for_date(self):
        rows = _parse({"results": [{"title": "T", "url": "http://z.com", "last_updated": "2026-01-01"}]})
        assert rows[0].date == "2026-01-01"

    def test_skips_missing_title_or_url(self):
        body = {"results": [
            {"title": "", "url": "http://x.com"},
            {"title": "No url", "url": ""},
            {"title": "Good", "url": "http://z.com"},
        ]}
        rows = _parse(body)
        assert len(rows) == 1
        assert rows[0].title == "Good"

    def test_non_dict_and_error_body(self):
        assert _parse(None) == []
        assert _parse({"error": "unauthorized"}) == []
        assert _parse("string") == []
