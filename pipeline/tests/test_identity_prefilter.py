"""Unit tests for the deterministic identity pre-filter — pure logic, no API.

Covers the slam-dunk auto-accept rule (name + company + one secondary anchor),
its conservatism (weak/partial signals fall through to Sonnet, nothing is ever
pre-rejected), token-level company/school matching across corporate suffixes and
word order, and the full-skip case where every source is decided."""
from __future__ import annotations

import pytest

from discovery import Source
from enrichment_store import DECISION_ACCEPT
from identity import AUTO_ACCEPT, PersonAnchors
from identity_prefilter import (
    _all_tokens_present,
    _is_slam_dunk,
    _phrase_present,
    prefilter,
)

_ANCHORS = PersonAnchors(
    full_name="Jane Doe",
    company="Acme Capital",
    city="Austin",
    school="Rice University",
    titan_class=12,
)


def _src(url: str, text: str) -> Source:
    return Source(url=url, title="", description="", markdown=text, relevance=0.5)


@pytest.mark.unit
def test_phrase_present_is_word_boundary_safe() -> None:
    assert _phrase_present("jane doe", "we met jane doe today")
    assert not _phrase_present("jane doe", "janedoexample")
    assert not _phrase_present("austin", "this is exhausting")


@pytest.mark.unit
def test_all_tokens_present_ignores_suffixes_and_order() -> None:
    # "Acme Capital" matches "Capital, Acme LLC" — order and suffixes stripped.
    assert _all_tokens_present("Acme Capital", _norm("Capital, Acme LLC"))
    assert not _all_tokens_present("Acme Capital", _norm("Beta Capital"))


def _norm(text: str) -> str:
    from identity_prefilter import _normalize

    return _normalize(text)


@pytest.mark.unit
def test_slam_dunk_requires_name_company_and_secondary() -> None:
    assert _is_slam_dunk(["name", "company", "city"], company_is_placeholder=False)
    assert _is_slam_dunk(["name", "company", "school"], company_is_placeholder=False)
    assert not _is_slam_dunk(["name", "company"], company_is_placeholder=False)  # no secondary
    assert not _is_slam_dunk(["name", "city", "school"], company_is_placeholder=False)  # no company
    assert not _is_slam_dunk(["company", "city", "school"], company_is_placeholder=False)  # no name


@pytest.mark.unit
def test_placeholder_company_never_slam_dunks() -> None:
    # When 'company' is just the school placeholder, even a full anchor set must
    # fall through to Sonnet — it's really name + one school token.
    assert not _is_slam_dunk(["name", "company", "school"], company_is_placeholder=True)
    assert not _is_slam_dunk(["name", "company", "city"], company_is_placeholder=True)


@pytest.mark.unit
def test_strong_match_is_auto_accepted_without_sonnet() -> None:
    source = _src(
        "https://acme.com/team",
        "Jane Doe is a partner at Acme Capital in Austin. She studied at Rice University.",
    )
    out = prefilter(_ANCHORS, (source,))
    assert out.ambiguous == ()
    assert len(out.decided) == 1
    v = out.decided[0]
    assert v.decision == DECISION_ACCEPT
    assert v.confidence >= AUTO_ACCEPT
    assert v.source_url == "https://acme.com/team"
    assert "name" in v.reason and "company" in v.reason


@pytest.mark.unit
def test_name_only_falls_through_to_sonnet() -> None:
    source = _src("https://news.com/x", "Jane Doe won an award last night.")
    out = prefilter(_ANCHORS, (source,))
    assert out.decided == ()
    assert [s.url for s in out.ambiguous] == ["https://news.com/x"]


@pytest.mark.unit
def test_name_plus_company_only_is_not_a_slam_dunk() -> None:
    # Two anchors is not enough — namesake risk; Sonnet must judge.
    source = _src("https://x.com", "Jane Doe joined Acme Capital this year.")
    out = prefilter(_ANCHORS, (source,))
    assert out.decided == ()
    assert len(out.ambiguous) == 1


@pytest.mark.unit
def test_prefilter_never_rejects() -> None:
    # A clearly-unrelated page is sent to Sonnet, not dropped.
    source = _src("https://random.com", "An article about marine biology in Norway.")
    out = prefilter(_ANCHORS, (source,))
    assert out.decided == ()
    assert len(out.ambiguous) == 1


@pytest.mark.unit
def test_mixed_batch_splits_decided_and_ambiguous() -> None:
    strong = _src(
        "https://acme.com",
        "Jane Doe, Acme Capital, based in Austin; Rice University alum.",
    )
    weak = _src("https://blog.com", "A post mentioning Jane Doe in passing.")
    out = prefilter(_ANCHORS, (strong, weak))
    assert [v.source_url for v in out.decided] == ["https://acme.com"]
    assert [s.url for s in out.ambiguous] == ["https://blog.com"]


# --- Namesake-contamination regressions (the "Austin Christensen" failure) ----

# Roster row for a low-footprint common name: no real employer (company is the
# school placeholder) and the FIRST NAME equals the city.
_WEAK_ANCHORS = PersonAnchors(
    full_name="Austin Christensen",
    company="University of Texas",   # placeholder — same as school
    city="Austin",                   # collides with the first name
    school="University of Texas",
    titan_class=5,
)


@pytest.mark.unit
def test_namesake_source_is_not_auto_accepted_for_weak_anchors() -> None:
    """A namesake SEC report (a DIFFERENT Austin Christensen) must NOT auto-accept:
    company is the school placeholder and the city only matches via the name."""
    namesake = _src(
        "https://reports.adviserinfo.sec.gov/individual_7474150.pdf",
        "Austin Christensen, registered representative at Cetera, Cedar City, "
        "Utah. Texas mentioned once in a disclosure.",
    )
    out = prefilter(_WEAK_ANCHORS, (namesake,))
    assert out.decided == ()                       # not auto-accepted
    assert len(out.ambiguous) == 1                 # routed to the Sonnet gate


@pytest.mark.unit
def test_city_equal_to_first_name_is_not_an_independent_anchor() -> None:
    from identity_prefilter import _matched_anchors, _source_text

    text = _source_text(
        _src("u", "Austin Christensen lives somewhere; University of Texas; Texas.")
    )
    matched = _matched_anchors(_WEAK_ANCHORS, text)
    assert "name" in matched
    assert "city" not in matched  # 'Austin' suppressed — it's the person's name


@pytest.mark.unit
def test_real_employer_still_auto_accepts_with_same_school() -> None:
    """The fix must not over-correct: a person with a REAL employer (distinct from
    the school) still auto-accepts on a full anchor match."""
    anchors = PersonAnchors(
        full_name="Nicholas Gagnet",
        company="Coatue",
        city="New York",
        school="University of Texas",
        titan_class=5,
    )
    source = _src(
        "https://coatue.com/team",
        "Nicholas Gagnet is an investor at Coatue in New York; University of Texas alum.",
    )
    out = prefilter(anchors, (source,))
    assert len(out.decided) == 1
    assert out.ambiguous == ()


@pytest.mark.unit
def test_empty_sources_is_safe() -> None:
    out = prefilter(_ANCHORS, ())
    assert out.decided == ()
    assert out.ambiguous == ()


# --- Data-broker echo regression (the "Ricardo Lopez" failure) ----------------
#
# Data-broker / people-directory pages parrot the search query (name, employer,
# city, school) in their title/boilerplate for SEO. Token-presence matching then
# "corroborates" anchors that the real profile body contradicts. The live failure:
# a UT/JP Morgan/Dallas roster row auto-accepted a wwana.com page that actually
# described a Boston University / Morgan Stanley / New York namesake — while Sonnet
# REJECTED the identical profile on sibling sources. Brokers must never auto-accept;
# they go to Sonnet, which reads semantically.

_BROKER_ANCHORS = PersonAnchors(
    full_name="Ricardo Lopez",
    company="JP Morgan",
    city="Dallas",
    school="University of Texas",
    titan_class=2,
)


@pytest.mark.unit
def test_data_broker_source_is_never_auto_accepted() -> None:
    # A full anchor match (name + company + city) on a known data-broker host must
    # fall through to Sonnet, not auto-accept on echoed boilerplate.
    broker = _src(
        "https://www.wwana.com/home/4831484-ricardo-lopez/profile",
        "Ricardo Lopez — JP Morgan — Dallas, Texas. Find Ricardo Lopez profile.",
    )
    out = prefilter(_BROKER_ANCHORS, (broker,))
    assert out.decided == ()
    assert [s.url for s in out.ambiguous] == [broker.url]


@pytest.mark.unit
def test_data_broker_subdomain_is_also_excluded() -> None:
    # Subdomains of a broker (app./profiles./api.) must not slip past the host check.
    broker = _src(
        "https://app.rocketreach.co/person/ricardo-lopez",
        "Ricardo Lopez, JP Morgan, Dallas, Texas.",
    )
    out = prefilter(_BROKER_ANCHORS, (broker,))
    assert out.decided == ()
    assert len(out.ambiguous) == 1


@pytest.mark.unit
def test_subdomain_only_broker_entries_are_matched() -> None:
    # Some broker entries in the set ARE sub-domains (signal.nfx.com,
    # advisor.investedbetter.com). Their registrable form (nfx.com / investedbetter.com)
    # is NOT in the set, so the check must test the FULL host too — these real hosts
    # appear in the live data and must never auto-accept.
    from identity_prefilter import is_untrusted_identity_host

    assert is_untrusted_identity_host("https://signal.nfx.com/investors/x")
    assert is_untrusted_identity_host("https://advisor.investedbetter.com/firm/x")
    # And the registrable-form match still works for bare data brokers + sub-domains.
    assert is_untrusted_identity_host("https://app.apollo.io/contact/x")
    assert not is_untrusted_identity_host("https://acme.com/team")


@pytest.mark.unit
def test_school_named_after_a_state_is_not_a_secondary_anchor() -> None:
    # "University of Texas" reduces to the single geographic token "texas", which
    # any Texas-related page satisfies. It must not count as the secondary anchor.
    from identity_prefilter import _matched_anchors, _source_text

    anchors = PersonAnchors(
        full_name="Ricardo Lopez",
        company="JP Morgan",
        city="Houston",  # a city the page does NOT mention
        school="University of Texas",
        titan_class=2,
    )
    # Non-broker page that mentions the name, employer, and the STATE — but not the
    # roster city and not the university itself.
    text = _source_text(
        _src("https://firm.com/x", "Ricardo Lopez at JP Morgan, based in Texas.")
    )
    matched = _matched_anchors(anchors, text)
    assert "name" in matched and "company" in matched
    assert "school" not in matched  # 'texas' alone is not a school anchor
    assert "city" not in matched


@pytest.mark.unit
def test_distinctive_school_token_still_counts() -> None:
    # The geo guard must not over-correct: "Baylor"/"Rice" are distinctive, not
    # geographic, and still anchor a slam dunk.
    from identity_prefilter import _matched_anchors, _source_text

    anchors = PersonAnchors(
        full_name="Jane Doe",
        company="Acme Capital",
        city="Nowhere",
        school="Baylor University",
        titan_class=4,
    )
    text = _source_text(
        _src("https://acme.com/x", "Jane Doe, Acme Capital, Baylor University alum.")
    )
    matched = _matched_anchors(anchors, text)
    assert "school" in matched
