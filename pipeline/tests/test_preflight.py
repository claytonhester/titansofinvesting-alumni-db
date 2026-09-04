"""Preflight's per-key auth probes — HTTP mocked, no spend."""
from __future__ import annotations

import httpx
import pytest

from preflight import (
    ANTHROPIC_MESSAGES_URL,
    AUTH_FAIL,
    AUTH_OK,
    AUTH_UNKNOWN,
    FIRECRAWL_CREDIT_USAGE_URL,
    check_key_auth,
)

_KEYS = ["ANTHROPIC_API_KEY", "FIRECRAWL_API_KEY", "PDL_API_KEY", "PERPLEXITY_API_KEY"]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.mark.unit
@pytest.mark.parametrize("name", _KEYS)
@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_is_auth_fail(name: str, status: int) -> None:
    check = check_key_auth(name, "bad", _client(lambda r: httpx.Response(status)))
    assert check.status == AUTH_FAIL


@pytest.mark.unit
@pytest.mark.parametrize("name,status", [
    ("ANTHROPIC_API_KEY", 200),
    ("FIRECRAWL_API_KEY", 200),
    ("PDL_API_KEY", 400),         # deliberately-invalid probe: 400 proves the key
    ("PERPLEXITY_API_KEY", 400),
])
def test_accepted_key_is_ok(name: str, status: int) -> None:
    check = check_key_auth(name, "good", _client(lambda r: httpx.Response(status)))
    assert check.status == AUTH_OK


@pytest.mark.unit
def test_server_error_or_outage_is_unknown_not_fail() -> None:
    assert check_key_auth("PDL_API_KEY", "k", _client(lambda r: httpx.Response(503))).status == AUTH_UNKNOWN

    def _down(r):
        raise httpx.ConnectError("dns")
    check = check_key_auth("PDL_API_KEY", "k", _client(_down))
    assert check.status == AUTH_UNKNOWN and "unreachable" in check.detail


@pytest.mark.unit
def test_probes_are_cheap_and_carry_the_key() -> None:
    seen: dict = {}

    def _spy(req: httpx.Request):
        seen[str(req.url)] = (req.method, dict(req.headers), req.read())
        return httpx.Response(200)

    http = _client(_spy)
    for name in _KEYS:
        check_key_auth(name, "sekrit", http)

    method, headers, body = seen[ANTHROPIC_MESSAGES_URL]
    assert method == "POST" and headers["x-api-key"] == "sekrit"
    assert b'"max_tokens": 1' in body  # one token, not a real completion
    method, headers, _ = seen[FIRECRAWL_CREDIT_USAGE_URL]
    assert method == "GET" and headers["authorization"] == "Bearer sekrit"
    # The two "invalid request" probes send no person/query: nothing to bill.
    pdl = next(v for k, v in seen.items() if "peopledatalabs" in k)
    assert pdl[1]["x-api-key"] == "sekrit" and b"name" not in pdl[2]
    pplx = next(v for k, v in seen.items() if "perplexity" in k)
    assert pplx[1]["authorization"] == "Bearer sekrit" and pplx[2] == b"{}"


@pytest.mark.unit
def test_unknown_key_name_is_reported_not_raised() -> None:
    check = check_key_auth("NOPE_KEY", "k", _client(lambda r: httpx.Response(200)))
    assert check.status == AUTH_UNKNOWN
