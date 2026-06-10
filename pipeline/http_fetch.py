"""Free best-effort article fetcher (no Firecrawl) for news verification.

Primary: Jina AI Reader (https://r.jina.ai/<url>) — a free, key-less service that
renders JS and returns clean LLM-friendly markdown for most pages (incl. YouTube),
which plain HTML scraping misses. Fallback: direct httpx + BeautifulSoup.

The news curator (news_curate) reads the returned text around the person's name to
confirm they are the article's SUBJECT — not name-dropped in someone else's story
(the Ross Willmann / Forty-Under-Forty misattribution). An unfetchable page (e.g.
Cloudflare-walled) returns "" so the curator treats it as unverifiable and drops the
item: precision over recall.

Never raises. SSRF guard on the target host (only public http/https). Jina is
rate-limited to ~20 req/min without a key, so calls are throttled client-side.
"""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TitansResearch/1.0; +research)"}
_MAX_CHARS = 20_000
_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript")
_MAX_REDIRECTS = 5

# Jina free tier ≈ 20 req/min without a key; stay just under with a client throttle.
_JINA_BASE = "https://r.jina.ai/"
_JINA_MIN_INTERVAL = 3.2
_last_jina_call = [0.0]
_jina_lock = threading.Lock()  # the throttle is correct even if a caller parallelizes
# Markers Jina returns when the TARGET blocked it (Cloudflare etc.) — treat as miss.
_BLOCK_MARKERS = ("Just a moment...", "Target URL returned error 4", "Enable JavaScript and cookies")


def _is_public_host(host: str) -> bool:
    """False for empty, loopback, private, link-local, or unresolvable hosts —
    blocks SSRF to internal services from an attacker-influenced URL."""
    if not host:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for *_, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            return False
    return True


def _via_jina(url: str, timeout: float) -> str:
    """Jina Reader markdown for a URL, throttled. '' on failure or a target block.

    SSRF-safe by construction: the only host WE connect to is r.jina.ai (public);
    Jina fetches the target server-side, so the target host never touches our
    network. follow_redirects stays on for Jina's own (public) redirects."""
    with _jina_lock:
        wait = _JINA_MIN_INTERVAL - (time.monotonic() - _last_jina_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_jina_call[0] = time.monotonic()
    try:
        resp = httpx.get(_JINA_BASE + url, headers={**_HEADERS, "Accept": "text/plain"},
                         timeout=timeout, follow_redirects=True)
    except Exception:
        return ""
    if resp.status_code != 200:
        return ""
    text = resp.text or ""
    if any(m in text[:600] for m in _BLOCK_MARKERS):
        return ""
    return text[:_MAX_CHARS]


def _via_httpx(url: str, timeout: float) -> str:
    """Direct fetch + BeautifulSoup text. '' on anything that isn't a clean HTML 200.

    Unlike Jina, this connects to the TARGET host from our machine, so a public URL
    that 30x-redirects to a private/internal address would be an SSRF. Redirects are
    therefore followed MANUALLY, re-validating each hop's host with _is_public_host;
    a non-public hop, a missing Location, or too many hops yields ''."""
    current = url
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = None
            for _ in range(_MAX_REDIRECTS + 1):
                if not _is_public_host(urlparse(current).hostname or ""):
                    return ""
                resp = client.get(current, headers=_HEADERS)
                if not resp.is_redirect:
                    break
                location = resp.headers.get("location")
                if not location:
                    return ""
                current = str(httpx.URL(current).join(location))
            else:
                return ""  # exhausted the redirect budget
    except Exception:
        return ""
    if resp is None or resp.status_code != 200:
        return ""
    if "html" not in resp.headers.get("content-type", "").lower():
        return ""
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        return " ".join(soup.get_text(separator=" ").split())[:_MAX_CHARS]
    except Exception:
        return ""


def fetch_article(url: str, *, timeout: float = 45.0) -> str:
    """Best-effort readable text for a URL: Jina Reader first, then direct httpx.
    '' when neither yields clean content — the caller treats '' as 'unverifiable'."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return ""
    if not _is_public_host(urlparse(url).hostname or ""):
        return ""
    return _via_jina(url, timeout) or _via_httpx(url, min(timeout, 20.0))
