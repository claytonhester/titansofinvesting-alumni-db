"""Tests for the free article fetcher's guards — the no-network paths.

Network success is not unit-tested (it hits live sites); these cover the safety
rails: bad input and the SSRF guard must return "" without raising."""
from __future__ import annotations

import pytest

from http_fetch import _is_public_host, fetch_article


@pytest.mark.unit
def test_non_http_and_empty_urls_return_empty() -> None:
    assert fetch_article("") == ""
    assert fetch_article("not a url") == ""
    assert fetch_article("ftp://example.com/x") == ""
    assert fetch_article("file:///etc/passwd") == ""


@pytest.mark.unit
def test_ssrf_guard_blocks_loopback_and_private_hosts() -> None:
    assert _is_public_host("127.0.0.1") is False
    assert _is_public_host("localhost") is False
    assert _is_public_host("10.0.0.5") is False
    assert _is_public_host("192.168.1.1") is False
    assert _is_public_host("169.254.169.254") is False  # cloud metadata endpoint
    assert _is_public_host("") is False


@pytest.mark.unit
def test_fetch_article_refuses_private_targets_without_network() -> None:
    # Even with a well-formed URL, a private/loopback target is refused up front.
    assert fetch_article("http://127.0.0.1/admin") == ""
    assert fetch_article("http://169.254.169.254/latest/meta-data/") == ""
