"""Unit tests for reconcile.py's pure logic (partition / parse / apply).

The single Haiku call in `reconcile_claims` is exercised live by the 50-person
validation run; here we lock down the deterministic plumbing that turns a model
response into reconciled ClaimRows, including the two non-negotiable safety
guarantees: never drop a claim, never touch public mentions.
"""
from __future__ import annotations

from enrichment_store import ClaimRow
from reconcile import (
    RECONCILE_METHOD_SUFFIX,
    _Decision,
    _apply,
    _build_user,
    _parse_decisions,
    _partition,
    _short_source,
    _significant_tokens,
)


def _claim(ct, value, src="", method="pdl", conf=0.8, quote=""):
    return ClaimRow(claim_type=ct, value=value, source_url=src, quote=quote,
                    confidence=conf, extraction_method=method)


def test_partition_keeps_mentions_out_of_reconciliation():
    claims = [
        _claim("career_history", "Analyst, TRS"),
        _claim("public_links", "Bio page", src="https://x.com"),
        _claim("current_employer", "TRS"),
        _claim("news_mention", "Some article"),
    ]
    resume, passthrough = _partition(claims)
    assert [c.claim_type for c in resume] == ["career_history", "current_employer"]
    assert [c.claim_type for c in passthrough] == ["public_links", "news_mention"]


def test_parse_decisions_clean():
    text = (
        '{"facts": [{"claim_type": "career_history", '
        '"value": "Investment Analyst, Teacher Retirement System (2015-2018)", '
        '"members": [0, 1], "primary": 1}]}'
    )
    d = _parse_decisions(text, 2)
    assert len(d) == 1
    assert d[0].members == (0, 1) and d[0].primary == 1


def test_parse_decisions_drops_out_of_range_indices():
    text = '{"facts": [{"claim_type": "education", "value": "X", "members": [0, 9], "primary": 9}]}'
    d = _parse_decisions(text, 2)
    # index 9 is dropped; primary 9 not in remaining members -> falls back to first.
    assert len(d) == 1 and d[0].members == (0,) and d[0].primary == 0


def test_parse_decisions_handles_fences_and_prose():
    text = 'Sure:\n```json\n{"facts": [{"claim_type": "location", "value": "Austin", "members": [0], "primary": 0}]}\n```'
    d = _parse_decisions(text, 1)
    assert len(d) == 1 and d[0].value == "Austin"


def test_parse_decisions_unusable_returns_empty():
    assert _parse_decisions("not json", 3) == []
    assert _parse_decisions('{"facts": "nope"}', 3) == []


def test_apply_never_drops_an_unmentioned_claim():
    resume = [
        _claim("career_history", "Analyst, TRS"),
        _claim("career_history", "Partner, Acme"),  # model forgets this one
    ]
    decisions = [_Decision("career_history", "Analyst, Teacher Retirement System", (0,), 0)]
    out = _apply(resume, decisions)
    values = sorted(c.value for c in out)
    # the forgotten claim survives verbatim; the grouped one is recased
    assert "Partner, Acme" in values
    assert any("Teacher Retirement System" in c.value for c in out)
    assert len(out) == 2


def test_apply_preserves_primary_provenance_and_marks_merge():
    resume = [
        _claim("career_history", "Analyst, TRS", src="https://aggregator.com", method="pdl"),
        _claim("career_history", "Investment Analyst at TRS 2015",
               src="https://trs.texas.gov/bio", method="claude-haiku-4-5-20251001", quote="...verbatim..."),
    ]
    decisions = [_Decision("career_history", "Investment Analyst, TRS (2015)", (0, 1), 1)]
    out = _apply(resume, decisions)
    assert len(out) == 1
    row = out[0]
    # primary=1 -> keep its source_url, quote; record BOTH contributing sources
    assert row.source_url == "https://trs.texas.gov/bio"
    assert row.quote == "...verbatim..."
    assert row.extraction_method.endswith(RECONCILE_METHOD_SUFFIX)
    assert "pdl" in row.extraction_method and "firecrawl" in row.extraction_method


def test_apply_does_not_mark_single_member_groups():
    resume = [_claim("current_employer", "trs", method="pdl")]
    decisions = [_Decision("current_employer", "TRS", (0,), 0)]
    out = _apply(resume, decisions)
    assert out[0].extraction_method == "pdl"  # no suffix for a 1-member group
    assert out[0].value == "TRS"  # recased via smart_title


def test_apply_leaves_short_bio_casing_untouched():
    bio = "He leads investments at a Texas pension fund."
    resume = [_claim("short_bio", bio, method="synthesis"),
              _claim("short_bio", "shorter bio", method="pdl")]
    decisions = [_Decision("short_bio", bio, (0, 1), 0)]
    out = _apply(resume, decisions)
    assert out[0].value == bio  # smart_title NOT applied to prose bios


def test_short_source_prefers_host_then_method():
    assert _short_source(_claim("x", "y", src="https://www.trs.texas.gov/bio")) == "trs.texas.gov"
    assert _short_source(_claim("x", "y", src="", method="pdl")) == "pdl"


def test_significant_tokens_strips_generic_role_words():
    # "Chief Financial Officer" is all generic -> the only identity is the company.
    assert _significant_tokens("Chief Financial Officer at Sitio Royalties") == {"sitio", "royalties"}
    assert _significant_tokens("Managing Director at Chambers Energy Capital (2009-2021)") == {"chambers", "energy"}


def test_apply_splits_wrong_merge_of_distinct_companies():
    # The model wrongly groups two different CFO jobs; the guard must split them
    # so neither company is erased.
    resume = [
        _claim("career_history", "Chief Financial Officer at Sitio Royalties (2021-2022)"),
        _claim("career_history", "Chief Financial Officer of Falcon Minerals"),
    ]
    decisions = [_Decision("career_history", "Chief Financial Officer at Sitio Royalties (2021-2022)", (0, 1), 0)]
    out = _apply(resume, decisions)
    blob = " | ".join(c.value for c in out)
    assert "Sitio" in blob and "Falcon" in blob  # both companies survive
    assert len(out) == 2


def test_apply_bad_canonical_matching_no_member_reemits_all():
    # Regression: the LLM wrongly groups two distinct firms AND names a THIRD in
    # the canonical value. The overlap guard would empty `absorbed`; _apply must
    # NOT crash (IndexError) — it re-emits every member verbatim, invents nothing.
    resume = [
        _claim("career_history", "Analyst at Morgan Stanley"),
        _claim("career_history", "Vice President at Barclays"),
    ]
    decisions = [_Decision("career_history", "Partner at Goldman Sachs", (0, 1), 0)]
    out = _apply(resume, decisions)
    vals = sorted(c.value for c in out)
    assert vals == ["Analyst at Morgan Stanley", "Vice President at Barclays"]
    assert not any("Goldman" in c.value for c in out)


def test_source_family_maps_methods():
    from reconcile import _source_family
    assert _source_family("pdl") == "pdl"
    assert _source_family("pdl+haiku-verify") == "pdl"
    assert _source_family("claude-haiku-4-5-20251001") == "firecrawl"
    assert _source_family("claude-haiku-4-5-synthesis") == "synthesis"
    assert _source_family("perplexity+haiku-verify") == "perplexity"
    assert _source_family("firecrawl-linkedin") == "firecrawl_linkedin"
    assert _source_family("firecrawl_news") == "firecrawl_news"


def test_apply_allows_merge_when_distinctive_token_shared():
    # Same company, two phrasings (one richer) -> genuine merge, one row out.
    resume = [
        _claim("career_history", "Managing Director and Investment Committee Member at Chambers Energy Capital"),
        _claim("career_history", "Managing Director at Chambers Energy Capital (2009-2021)"),
    ]
    decisions = [_Decision(
        "career_history",
        "Managing Director and Investment Committee Member at Chambers Energy Capital (2009-2021)",
        (0, 1), 0,
    )]
    out = _apply(resume, decisions)
    assert len(out) == 1
    assert "Chambers" in out[0].value and "2009-2021" in out[0].value


def test_build_user_numbers_every_claim():
    resume = [_claim("career_history", "A"), _claim("education", "B")]
    user = _build_user(resume)
    assert "[0] career_history | A" in user
    assert "[1] education | B" in user


# --- dated/recent tiebreaker (career groups) -----------------------------------

def _decision(ct, value, members, primary):
    from reconcile import _Decision
    return _Decision(ct, value, tuple(members), primary)


def test_tiebreak_undated_canonical_upgraded_to_dated_member():
    """The Bart Howe case: the model crowns a stale undated phrasing while a
    member carries the full dated LinkedIn version — the dated value must win."""
    resume = [
        _claim("career_history", "Co-founder of Holland Course Capital",
               src="https://theorg.com/x", method="firecrawl"),
        _claim("career_history",
               "Co-Founder & Managing Partner at Holland Course Capital (2017-present)",
               src="https://linkedin.com/in/bart", method="firecrawl-linkedin"),
    ]
    out = _apply(resume, [_decision(
        "career_history", "Co-founder of Holland Course Capital", [0, 1], 0)])
    assert len(out) == 1
    assert "(2017-Present)".lower() in out[0].value.lower()
    assert out[0].source_url == "https://linkedin.com/in/bart"


def test_tiebreak_prefers_most_recent_dated_member_provenance():
    """When the canonical IS dated, provenance routes to the dated member that
    attests it, not an undated aggregator page."""
    resume = [
        _claim("career_history", "EVP at Caris Life Sciences",
               src="https://aggregator.com/x"),
        _claim("career_history", "EVP at Caris Life Sciences (2014-2017)",
               src="https://linkedin.com/in/bart", method="firecrawl-linkedin"),
    ]
    out = _apply(resume, [_decision(
        "career_history", "EVP at Caris Life Sciences (2014-2017)", [0, 1], 0)])
    assert len(out) == 1
    assert out[0].source_url == "https://linkedin.com/in/bart"


def test_tiebreak_open_ended_beats_closed_when_canonical_undated():
    resume = [
        _claim("career_history", "Director at Acme (2010-2014)", src="https://a.com"),
        _claim("career_history", "Director at Acme (2014-present)", src="https://b.com"),
        _claim("career_history", "Director at Acme", src="https://c.com"),
    ]
    out = _apply(resume, [_decision("career_history", "Director at Acme", [0, 1, 2], 2)])
    assert len(out) == 1
    assert "present" in out[0].value.lower()
    assert out[0].source_url == "https://b.com"


def test_tiebreak_all_undated_keeps_model_choice():
    resume = [
        _claim("career_history", "Analyst at Acme", src="https://a.com"),
        _claim("career_history", "Acme Analyst", src="https://b.com"),
    ]
    out = _apply(resume, [_decision("career_history", "Analyst at Acme", [0, 1], 1)])
    assert len(out) == 1
    assert out[0].value == "Analyst at Acme"
    assert out[0].source_url == "https://b.com"  # model's primary respected


def test_tiebreak_only_touches_career_groups():
    resume = [
        _claim("current_employer", "Acme Capital", src="https://a.com"),
        _claim("current_employer", "Acme Capital LLC", src="https://b.com"),
    ]
    out = _apply(resume, [_decision("current_employer", "Acme Capital", [0, 1], 0)])
    assert len(out) == 1
    assert out[0].source_url == "https://a.com"  # model's primary untouched


def test_tiebreak_blended_dated_canonical_keeps_model_primary():
    """Quote/value-mismatch guard: when the canonical is dated but its text
    matches NO single member (a blend of title from one + dates from another),
    provenance stays with the model's primary — never routed to a member whose
    quote wouldn't attest the blended value."""
    resume = [
        _claim("career_history", "Analyst at TRS", src="https://web.com",
               quote="works as analyst", method="firecrawl"),
        _claim("career_history", "Senior Analyst, Teachers Retirement (2015-2018)",
               src="https://linkedin.com/in/x", quote="Senior Analyst 2015-2018",
               method="firecrawl-linkedin"),
    ]
    # Model blends: title-ish from neither verbatim, dates from member 1.
    out = _apply(resume, [_decision(
        "career_history", "Senior Analyst at TRS (2015-2018)", [0, 1], 0)])
    assert len(out) == 1
    # canonical != either member verbatim -> keep model primary (member 0)
    assert out[0].source_url == "https://web.com"


def test_tiebreak_is_convergent_across_two_passes():
    """Reconciling the runner's output again must be idempotent (linkedin_refresh
    reconciles the full set on every run). Simulate the second pass on the dated
    value the first pass emits and assert it is stable."""
    resume = [
        _claim("career_history", "Co-founder of Holland Course Capital",
               src="https://theorg.com/x", method="firecrawl"),
        _claim("career_history",
               "Co-Founder & Managing Partner at Holland Course Capital (2017-present)",
               src="https://linkedin.com/in/bart", method="firecrawl-linkedin"),
    ]
    first = _apply(resume, [_decision(
        "career_history", "Co-founder of Holland Course Capital", [0, 1], 0)])
    assert len(first) == 1
    # Second pass: the emitted dated value is now a member; the model again
    # (worst case) crowns an undated phrasing. Output must equal the first pass.
    second_in = [
        first[0],
        _claim("career_history", "Co-founder of Holland Course Capital",
               src="https://theorg.com/x", method="firecrawl"),
    ]
    second = _apply(second_in, [_decision(
        "career_history", "Co-founder of Holland Course Capital", [0, 1], 0)])
    assert len(second) == 1
    assert second[0].value == first[0].value
    assert second[0].source_url == first[0].source_url
