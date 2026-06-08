"""Unit tests for news_score precision heuristics."""
import pytest

from news_score import (
    employer_comention,
    is_aggregator_domain,
    is_finance_domain,
    normalize_domain,
    score_mention,
)


class TestNormalizeDomain:
    @pytest.mark.parametrize("raw,expected", [
        ("https://www.bloomberg.com/news/x", "bloomberg.com"),
        ("www.wsj.com", "wsj.com"),
        ("reuters.com", "reuters.com"),
        ("http://sub.example.com/path", "sub.example.com"),
    ])
    def test_normalize(self, raw, expected):
        assert normalize_domain(raw) == expected


class TestFinanceDomain:
    def test_known_finance_domain(self):
        assert is_finance_domain("https://www.bloomberg.com/x") is True

    def test_unknown_domain(self):
        assert is_finance_domain("randomblog.net") is False


class TestAggregatorDomain:
    def test_known_aggregator(self):
        assert is_aggregator_domain("https://www.idcrawl.com/x") is True

    def test_subdomain_matches(self):
        assert is_aggregator_domain("es.marketscreener.com") is True

    def test_normal_domain_not_aggregator(self):
        assert is_aggregator_domain("bloomberg.com") is False


class TestEmployerComention:
    def test_significant_token_present(self):
        assert employer_comention("Kaspar Companies expands", "Kaspar Companies") is True

    def test_generic_only_employer_no_match(self):
        # "Capital Group" -> all generic; must not match arbitrary text.
        assert employer_comention("A capital idea for the group", "Capital Group") is False

    def test_absent_employer(self):
        assert employer_comention("Some headline", "Crestline Investors") is False

    def test_no_employer(self):
        assert employer_comention("anything", None) is False


class TestScoreMention:
    def test_name_and_employer_cooccur_is_confident(self):
        s = score_mention(
            name="Jason Kaspar", employer="Kaspar Companies",
            title="Jason Kaspar named CEO of Kaspar Companies",
            snippet="", domain="bizjournals.com",
        )
        assert s.name_present is True
        assert s.employer_comention is True
        assert s.confident is True

    def test_name_on_finance_page_without_employer_is_plausible_not_confident(self):
        # Named on a finance outlet, but the known employer isn't there to
        # confirm it's the right person -> needs verification, not auto-trust.
        s = score_mention(
            name="Jane Roe", employer="Crestline Investors",
            title="Jane Roe joins new fund", snippet="A new vehicle launched",
            domain="bloomberg.com",
        )
        assert s.confident is False
        assert s.plausible is True

    def test_company_page_without_person_name_not_confident(self):
        # Mentions the firm but not the person -> not about them.
        s = score_mention(
            name="Sam Totusek", employer="Brightstar Capital Partners",
            title="Brightstar Capital Partners | BBB Business Profile",
            snippet="Brightstar Capital Partners company profile",
            domain="bbb.org",
        )
        assert s.name_present is False
        assert s.confident is False
        assert s.plausible is False

    def test_aggregator_domain_never_confident(self):
        # Even with name + employer, a people-search page is demoted.
        s = score_mention(
            name="Sam Totusek", employer="Brightstar Capital Partners",
            title="Sam Totusek - Brightstar Capital Partners",
            snippet="Sam Totusek at Brightstar Capital Partners",
            domain="idcrawl.com",
        )
        assert s.aggregator_domain is True
        assert s.confident is False
        assert s.plausible is False

    def test_namesake_random_blog_not_confident(self):
        s = score_mention(
            name="Jane Roe", employer="Crestline Investors",
            title="Local bakery wins award", snippet="A sweet shop story",
            domain="townblog.net",
        )
        assert s.name_present is False
        assert s.confident is False
        assert s.plausible is False
        assert s.score == 0
