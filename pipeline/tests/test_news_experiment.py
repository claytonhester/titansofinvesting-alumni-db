"""Unit tests for the news experiment harness's pure helpers."""
from news_experiment import even_sample
from gnews_enrich import build_query as gnews_build_query


class TestEvenSample:
    def test_returns_all_when_limit_exceeds_pool(self):
        assert even_sample([1, 2, 3], 10) == [1, 2, 3]

    def test_spreads_across_range(self):
        ids = list(range(1, 101))  # 1..100
        picked = even_sample(ids, 10)
        assert len(picked) == 10
        assert picked[0] == 1
        assert picked[-1] >= 90  # last pick is near the end, not clustered early

    def test_offset_skips_prefix(self):
        ids = list(range(1, 21))
        picked = even_sample(ids, 5, offset=10)
        assert all(i > 10 for i in picked)

    def test_empty_and_zero(self):
        assert even_sample([], 5) == []
        assert even_sample([1, 2, 3], 0) == []

    def test_deterministic(self):
        ids = list(range(1, 51))
        assert even_sample(ids, 7) == even_sample(ids, 7)


class TestGnewsQuery:
    def test_name_only(self):
        assert gnews_build_query("Jane Roe") == '"Jane Roe"'

    def test_name_and_employer(self):
        assert gnews_build_query("Jane Roe", "Longhorn Ventures") == '"Jane Roe" AND "Longhorn Ventures"'

    def test_generic_employer_falls_back_to_name(self):
        assert gnews_build_query("Jane Roe", "Capital Group") == '"Jane Roe"'

    def test_empty_name(self):
        assert gnews_build_query("  ") == ""
