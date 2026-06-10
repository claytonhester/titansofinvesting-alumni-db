"""Tests for the free article fetcher's guards — the no-network paths.

Network success is not unit-tested (it hits live sites); these cover the safety
rails: bad input and the SSRF guard must return "" without raising."""
from __future__ import annotations

import httpx
import pytest

import http_fetch
from http_fetch import _is_public_host, _via_httpx, fetch_article


class _FakeClient:
    """A drop-in for httpx.Client whose .get returns scripted responses, recording
    the URLs it was asked to fetch — so a redirect to a private host is observable."""

    def __init__(self, responses, seen):
        self._responses = list(responses)
        self._seen = seen

    def __init_subclass__(cls, **kw):  # pragma: no cover - defensive
        super().__init_subclass__(**kw)

    def __call__(self, **_kw):  # httpx.Client(timeout=..., follow_redirects=...)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False

    def get(self, url, headers=None):
        self._seen.append(url)
        resp = self._responses.pop(0)
        resp._request = httpx.Request("GET", url)
        return resp


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


@pytest.mark.unit
def test_via_httpx_blocks_redirect_to_private_host(monkeypatch) -> None:
    """A public origin that 30x-redirects to a private IP must NOT be fetched — the
    SSRF that follow_redirects=True would have allowed."""
    seen: list[str] = []
    # Only the origin is public; the redirect target is treated as private.
    monkeypatch.setattr(http_fetch, "_is_public_host", lambda host: host == "news.example.com")
    redirect = httpx.Response(302, headers={"location": "http://10.0.0.5/secret"})
    monkeypatch.setattr(http_fetch.httpx, "Client", _FakeClient([redirect], seen))
    assert _via_httpx("https://news.example.com/a", 5.0) == ""
    assert seen == ["https://news.example.com/a"]  # never connected to the private host


@pytest.mark.unit
def test_via_httpx_follows_public_redirect_then_parses(monkeypatch) -> None:
    """A redirect between two PUBLIC hosts is followed, and the final HTML is parsed."""
    seen: list[str] = []
    monkeypatch.setattr(http_fetch, "_is_public_host", lambda host: True)
    redirect = httpx.Response(301, headers={"location": "https://www.example.com/final"})
    page = httpx.Response(200, headers={"content-type": "text/html"},
                          text="<html><body><p>Hello world</p></body></html>")
    monkeypatch.setattr(http_fetch.httpx, "Client", _FakeClient([redirect, page], seen))
    out = _via_httpx("https://example.com/a", 5.0)
    assert "Hello world" in out
    assert seen == ["https://example.com/a", "https://www.example.com/final"]


@pytest.mark.unit
def test_via_httpx_stops_after_too_many_redirects(monkeypatch) -> None:
    """An endless redirect loop is bounded and yields '' rather than hanging."""
    seen: list[str] = []
    monkeypatch.setattr(http_fetch, "_is_public_host", lambda host: True)
    loop = [httpx.Response(302, headers={"location": "https://example.com/next"})
            for _ in range(20)]
    monkeypatch.setattr(http_fetch.httpx, "Client", _FakeClient(loop, seen))
    assert _via_httpx("https://example.com/start", 5.0) == ""
    assert len(seen) <= 7  # bounded by _MAX_REDIRECTS + 1
