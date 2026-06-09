"""Tests for the shared host classification + a drift guard.

directory_hosts.py is the single source of truth for broker/social/records hosts.
These tests assert each consumer module composes its list FROM that core, so a new
broker added in one place can't silently diverge across the pipeline again."""
from __future__ import annotations

import pytest

from directory_hosts import (
    DIRECTORY_HOSTS,
    PUBLIC_RECORDS_HOSTS,
    SOCIAL_HOSTS,
    full_host,
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
