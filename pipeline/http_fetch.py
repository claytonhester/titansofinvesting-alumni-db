"""Free best-effort article fetcher (no Firecrawl): httpx GET + BeautifulSoup text.

Restores the article-read verification step when Firecrawl is unavailable. The news
curator (news_curate) reads the returned text around the person's name to confirm
they are the article's SUBJECT — not name-dropped in someone else's story (the Ross
Willmann / Forty-Under-Forty misattribution). An unfetchable page returns "" so the
curator treats the item as unverifiable and drops it: precision over recall.

Never raises. SSRF guard: only http/https public hosts — private/loopback/link-local
targets are refused, since the URLs come from scraped/model output.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; TitansResearch/1.0; +research)"}
_MAX_CHARS = 20_000
_STRIP_TAGS = ("script", "style", "nav", "footer", "header", "aside", "form", "noscript")


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


def fetch_article(url: str, *, timeout: float = 20.0) -> str:
    """Best-effort readable text for a URL. '' on anything that isn't a clean,
    public, HTML 200 — the caller treats '' as 'could not verify'."""
    if not url or not url.lower().startswith(("http://", "https://")):
        return ""
    host = urlparse(url).hostname or ""
    if not _is_public_host(host):
        return ""
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=timeout, follow_redirects=True)
    except Exception:
        return ""
    if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "").lower():
        return ""
    try:
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(_STRIP_TAGS):
            tag.decompose()
        text = " ".join(soup.get_text(separator=" ").split())
    except Exception:
        return ""
    return text[:_MAX_CHARS]
