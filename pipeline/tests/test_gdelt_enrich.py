"""Unit tests for the GDELT adapter's pure logic (query building + parsing)."""
import json

import pytest

from gdelt_enrich import GdeltArticle, _employer_is_meaningful, _parse_articles, build_query


class TestBuildQuery:
    def test_name_only(self):
        assert build_query("Jane Roe", lang="") == '"Jane Roe"'

    def test_name_with_language(self):
        assert build_query("Jane Roe") == '"Jane Roe" sourcelang:english'

    def test_name_and_meaningful_employer(self):
        q = build_query("Jane Roe", "Longhorn Ventures", lang="")
        assert q == '"Jane Roe" "Longhorn Ventures"'

    def test_generic_only_employer_is_dropped(self):
        # "Capital Group" is all generic tokens -> not meaningful, name only.
        assert build_query("Jane Roe", "Capital Group", lang="") == '"Jane Roe"'

    def test_unknown_employer_dropped(self):
        assert build_query("Jane Roe", "(unknown)", lang="") == '"Jane Roe"'

    def test_empty_name_returns_empty(self):
        assert build_query("   ", "Acme Capital") == ""


class TestEmployerMeaningful:
    @pytest.mark.parametrize("emp", ["Kaspar", "Longhorn Ventures", "Goldman Sachs"])
    def test_meaningful(self, emp):
        assert _employer_is_meaningful(emp) is True

    @pytest.mark.parametrize("emp", [None, "", "(unknown)", "Capital", "the group", "LLC"])
    def test_not_meaningful(self, emp):
        assert _employer_is_meaningful(emp) is False


class TestParseArticles:
    def test_parses_valid_payload(self):
        body = json.dumps({"articles": [
            {"title": "Fund news", "url": "http://x.com/a", "domain": "x.com",
             "seendate": "20240101T120000Z", "language": "English"},
            {"title": "More", "url": "http://y.com/b", "domain": "y.com"},
        ]})
        arts = _parse_articles(body)
        assert len(arts) == 2
        assert isinstance(arts[0], GdeltArticle)
        assert arts[0].title == "Fund news"
        assert arts[0].domain == "x.com"

    def test_rate_limit_body_yields_empty(self):
        assert _parse_articles("Please limit requests to one every 5 seconds") == []

    def test_empty_body_yields_empty(self):
        assert _parse_articles("") == []
        assert _parse_articles("   ") == []

    def test_malformed_json_yields_empty(self):
        assert _parse_articles("{not json") == []

    def test_articles_missing_title_or_url_skipped(self):
        body = json.dumps({"articles": [
            {"title": "", "url": "http://x.com/a"},
            {"title": "No url", "url": ""},
            {"title": "Good", "url": "http://z.com/c", "domain": "z.com"},
        ]})
        arts = _parse_articles(body)
        assert len(arts) == 1
        assert arts[0].title == "Good"
