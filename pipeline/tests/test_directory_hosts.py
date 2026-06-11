"""Tests for the shared host classification + a drift guard.

directory_hosts.py is the single source of truth for broker/social/records hosts.
These tests assert each consumer module composes its list FROM that core, so a new
broker added in one place can't silently diverge across the pipeline again."""
from __future__ import annotations

import pytest

from directory_hosts import (
    DIRECTORY_HOSTS,
    NON_NEWS_HOSTS,
    PUBLIC_RECORDS_HOSTS,
    SOCIAL_HOSTS,
    full_host,
    is_non_news_host,
    is_untrusted_identity_host,
    registrable_host,
)


@pytest.mark.unit
def test_full_host_strips_scheme_path_www_and_keeps_subdomains() -> None:
    assert full_host("https://www.apollo.io/x") == "apollo.io"
    assert full_host("https://app.apollo.io/contact/x") == "app.apollo.io"
    assert full_host("apollo.io") == "apollo.io"


@pytest.mark.unit
def test_registrable_host_collapses_subdomains() -> None:
    assert registrable_host("https://app.apollo.io/x") == "apollo.io"
    assert registrable_host("profiles.zoominfo.com") == "zoominfo.com"
    assert registrable_host("apollo.io") == "apollo.io"


@pytest.mark.unit
def test_host_helpers_handle_malformed_input_without_raising() -> None:
    assert full_host("") == ""
    assert full_host("not a url") == "not a url"  # urlparse treats it as a host
    assert registrable_host("") == ""
    assert is_untrusted_identity_host("") is False
    assert is_untrusted_identity_host("https://user:pass@apollo.io:8080/x") is True


@pytest.mark.unit
def test_untrusted_identity_matches_bare_domains_subdomains_and_set_subdomains() -> None:
    assert is_untrusted_identity_host("https://apollo.io/x")            # bare domain
    assert is_untrusted_identity_host("https://app.apollo.io/x")        # sub of bare
    assert is_untrusted_identity_host("https://signal.nfx.com/x")       # set sub-domain
    assert is_untrusted_identity_host("https://advisor.investedbetter.com/x")
    assert not is_untrusted_identity_host("https://acme.com/team")      # real firm


@pytest.mark.unit
def test_non_news_host_blocks_brokers_and_records_with_subdomains() -> None:
    """Regression (Ricardo Lopez / wwana.com): broker/SEO-echo directories and
    public-records hosts are never news, on bare domains and sub-domains alike."""
    assert is_non_news_host("https://wwana.com/profile/ricardo-lopez")
    assert is_non_news_host("https://www.wwana.com/profile/ricardo-lopez")
    assert is_non_news_host("https://profiles.zoominfo.com/p/x")   # broker sub-domain
    assert is_non_news_host("https://govsalaries.com/x")           # public records
    assert not is_non_news_host("https://www.barrons.com/articles/x")  # real press
    assert not is_non_news_host("")


@pytest.mark.unit
def test_non_news_core_covers_directories_and_records() -> None:
    assert NON_NEWS_HOSTS == DIRECTORY_HOSTS | PUBLIC_RECORDS_HOSTS
    assert "wwana.com" in NON_NEWS_HOSTS


@pytest.mark.unit
def test_news_score_aggregator_set_extends_the_shared_core() -> None:
    # Drift guard: the Sonar news gate (is_aggregator_domain) must know every
    # shared broker/records host. news_score once kept its own copy, and a
    # wwana.com SEO-echo page became a news_mention claim + curated row even
    # though the identity gate had rejected the same host.
    from news_score import _AGGREGATOR_DOMAINS, is_aggregator_domain

    assert NON_NEWS_HOSTS <= _AGGREGATOR_DOMAINS
    assert is_aggregator_domain("https://www.wwana.com/profile/ricardo-lopez")


@pytest.mark.unit
def test_news_curate_directory_set_extends_the_shared_core() -> None:
    # Drift guard: the shared broker core must flow into the news directory set.
    from news_curate import _DIRECTORY_HOSTS, _PUBLIC_RECORDS_HOSTS, _SOCIAL_HOSTS

    assert DIRECTORY_HOSTS <= _DIRECTORY_HOSTS
    assert _SOCIAL_HOSTS == SOCIAL_HOSTS
    assert _PUBLIC_RECORDS_HOSTS == PUBLIC_RECORDS_HOSTS


@pytest.mark.unit
def test_company_enrich_nonfirm_set_extends_the_shared_core() -> None:
    # Drift guard: every shared broker/social host is a non-firm host too.
    from company_enrich import _NON_FIRM_HOSTS

    assert (DIRECTORY_HOSTS | SOCIAL_HOSTS) <= _NON_FIRM_HOSTS
