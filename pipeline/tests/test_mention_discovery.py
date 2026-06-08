"""Unit tests for mention_discovery's pure claim-building logic."""
from mention_discovery import CLAIM_TYPE, EXTRACTION_METHOD, MENTION_CONFIDENCE, _to_claim_rows
from perplexity_enrich import PerplexityResult


def _r(title: str, url: str, snippet: str = "snip") -> PerplexityResult:
    return PerplexityResult(title=title, url=url, snippet=snippet, date="2025-01-01")


class TestToClaimRows:
    def test_keeps_only_matches(self):
        results = [
            _r("Cason Beckham - Waterloo", "http://waterloo-associates.com/x"),
            _r("Cason Beckham the rancher", "http://ranchnews.com/y"),
        ]
        rows = _to_claim_rows(results, [True, False])
        assert len(rows) == 1
        assert rows[0].source_url == "http://waterloo-associates.com/x"
        assert rows[0].claim_type == CLAIM_TYPE
        assert rows[0].extraction_method == EXTRACTION_METHOD
        assert rows[0].confidence == MENTION_CONFIDENCE

    def test_value_is_title_and_quote_is_snippet(self):
        rows = _to_claim_rows([_r("Title Here", "http://x.com", "the snippet")], [True])
        assert rows[0].value == "Title Here"
        assert rows[0].quote == "the snippet"

    def test_all_rejected_yields_empty(self):
        rows = _to_claim_rows([_r("A", "http://x.com"), _r("B", "http://y.com")], [False, False])
        assert rows == []

    def test_empty_input(self):
        assert _to_claim_rows([], []) == []
