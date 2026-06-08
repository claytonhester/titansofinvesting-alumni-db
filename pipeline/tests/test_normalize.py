"""Tests for normalize.smart_title and normalize.digest_claims."""
import pytest
from enrichment_store import ClaimRow
from normalize import smart_title, digest_claims


def _claim(claim_type: str, value: str, confidence: float = 0.8, method: str = "haiku") -> ClaimRow:
    return ClaimRow(claim_type=claim_type, value=value, source_url="https://example.com",
                    quote="verbatim quote", confidence=confidence, extraction_method=method)


# ── smart_title ──────────────────────────────────────────────────────────────

class TestSmartTitle:
    def test_basic_lowercase(self):
        assert smart_title("adjunct professor") == "Adjunct Professor"

    def test_minor_words_stay_lower(self):
        assert smart_title("director of private equity") == "Director of Private Equity"

    def test_first_word_always_capitalized(self):
        assert smart_title("of mice and men") == "Of Mice and Men"

    def test_preserves_all_caps_acronym(self):
        assert smart_title("partner at KKR") == "Partner at KKR"

    def test_preserves_multiple_acronyms(self):
        assert smart_title("VP at TRS LLC") == "VP at TRS LLC"

    def test_preserves_mixed_case(self):
        assert smart_title("partner at McCallum Capital") == "Partner at McCallum Capital"

    def test_title_with_company(self):
        result = smart_title("senior investment manager at teacher retirement system of texas")
        assert result == "Senior Investment Manager at Teacher Retirement System of Texas"

    def test_empty_string(self):
        assert smart_title("") == ""

    def test_already_correct(self):
        assert smart_title("Managing Director") == "Managing Director"

    def test_punctuation_preserved(self):
        result = smart_title("director, private equity principal investments")
        assert result == "Director, Private Equity Principal Investments"

    def test_location(self):
        assert smart_title("austin, texas") == "Austin, Texas"

    def test_roman_numeral_suffix_uppercased(self):
        assert smart_title("partner iii") == "Partner III"

    def test_roman_numeral_already_uppercase_preserved(self):
        assert smart_title("Managing Director II") == "Managing Director II"

    def test_roman_numeral_with_punctuation(self):
        assert smart_title("director, iv") == "Director, IV"

    def test_single_all_caps_letter_not_treated_as_acronym(self):
        # len-1 tokens fall through to capitalize; "a" stays a minor word mid-string.
        assert smart_title("plan b strategy") == "Plan B Strategy"

    def test_internal_whitespace_collapsed(self):
        assert smart_title("managing    director") == "Managing Director"

    def test_leading_trailing_whitespace_stripped(self):
        assert smart_title("  managing director  ") == "Managing Director"


# ── digest_claims ────────────────────────────────────────────────────────────

class TestDigestClaims:
    def test_deduplicates_same_value_different_case(self):
        claims = [
            _claim("career_history", "Senior Investment Manager at TRS (2018-2020)", 0.7, "haiku"),
            _claim("career_history", "senior investment manager at TRS (2018-2020)", 0.6, "pdl"),
        ]
        result = digest_claims(claims)
        assert len(result) == 1
        assert result[0].value == "Senior Investment Manager at TRS (2018-2020)"

    def test_keeps_higher_confidence_on_dedup(self):
        claims = [
            _claim("current_title", "managing director", 0.6, "pdl"),
            _claim("current_title", "Managing Director", 0.9, "haiku"),
        ]
        result = digest_claims(claims)
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_normalizes_casing(self):
        claims = [_claim("current_title", "adjunct professor")]
        result = digest_claims(claims)
        assert result[0].value == "Adjunct Professor"

    def test_quote_field_untouched(self):
        claims = [_claim("current_title", "adjunct professor")]
        result = digest_claims(claims)
        assert result[0].quote == "verbatim quote"

    def test_news_mentions_not_deduplicated(self):
        claims = [
            _claim("news_mention", "article about investment", 0.0, "gnews"),
            _claim("news_mention", "article about investment", 0.0, "gnews"),
        ]
        result = digest_claims(claims)
        assert len(result) == 2

    def test_news_not_title_cased(self):
        claims = [_claim("news_mention", "hedge fund raises $500m in new round", 0.0, "gnews")]
        result = digest_claims(claims)
        assert result[0].value == "hedge fund raises $500m in new round"

    def test_short_bio_not_title_cased(self):
        claims = [_claim("short_bio", "he is a managing partner focused on energy.")]
        result = digest_claims(claims)
        assert result[0].value == "he is a managing partner focused on energy."

    def test_none_confidence_does_not_crash_and_floors_to_zero(self):
        # A malformed source could hand us confidence=None; it must never raise
        # and must lose to any real numeric confidence regardless of order.
        none_first = [
            _claim("current_title", "managing director", None, "bad"),  # type: ignore[arg-type]
            _claim("current_title", "Managing Director", 0.9, "haiku"),
        ]
        result = digest_claims(none_first)
        assert len(result) == 1
        assert result[0].confidence == 0.9

    def test_none_confidence_when_only_value(self):
        claims = [_claim("current_title", "managing director", None, "bad")]  # type: ignore[arg-type]
        result = digest_claims(claims)
        assert len(result) == 1
        assert result[0].value == "Managing Director"

    def test_internal_whitespace_dedupes_after_normalize(self):
        # Title-case types collapse internal whitespace, so spacing variants merge.
        claims = [
            _claim("current_title", "managing    director", 0.7, "haiku"),
            _claim("current_title", "Managing Director", 0.6, "pdl"),
        ]
        result = digest_claims(claims)
        assert len(result) == 1

    def test_mixed_types_preserved(self):
        claims = [
            _claim("current_title", "managing partner"),
            _claim("current_employer", "kayne anderson"),
            _claim("career_history", "analyst at goldman sachs (2010-2012)"),
            _claim("news_mention", "kayne anderson raises fund", 0.0),
        ]
        result = digest_claims(claims)
        assert len(result) == 4
        titles = {c.claim_type: c.value for c in result}
        assert titles["current_title"] == "Managing Partner"
        assert titles["current_employer"] == "Kayne Anderson"
        assert titles["career_history"] == "Analyst at Goldman Sachs (2010-2012)"
        assert titles["news_mention"] == "kayne anderson raises fund"
