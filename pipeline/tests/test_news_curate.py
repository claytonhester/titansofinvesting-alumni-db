"""Unit tests for news_curate: value parsing, fallback, and model merge."""
from __future__ import annotations

from types import SimpleNamespace

from enrichment_store import ClaimRow
from news_curate import _split_value, curate_news


def _mention(value, quote="", url="https://www.bloomberg.com/x"):
    return ClaimRow("news_mention", value, url, quote, 0.8, "firecrawl_news")


def test_split_value_with_iso_date():
    assert _split_value("2026-05-21 — Fund Closes $1B") == ("2026-05-21", "Fund Closes $1B")
    assert _split_value("No date here") == ("", "No date here")


def test_no_mentions_returns_empty():
    assert curate_news(None, "Jane", "Acme", []) == ([], 0, 0)
    other = [ClaimRow("current_title", "CEO", "", "", 0.9, "pdl")]
    assert curate_news(None, "Jane", "Acme", other) == ([], 0, 0)


def test_fallback_without_client():
    mentions = [_mention("2026-05-21 — Acme Raises $1B", "Acme closed its fund.")]
    curated, ti, to = curate_news(None, "Jane", "Acme", mentions)
    assert ti == 0 and to == 0 and len(curated) == 1
    c = curated[0]
    assert c.headline == "Acme Raises $1B" and c.date == "2026-05-21"
    assert c.category == "Company News"          # neutral fallback
    assert c.summary == "Acme closed its fund."  # snippet as summary
    assert c.source_host == "bloomberg.com"      # www. stripped
    assert 0.0 <= c.importance <= 1.0


def _client(text):
    def create(**_):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=12, output_tokens=6),
        )
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_client_assigns_category_summary_importance():
    mentions = [
        _mention("2026-05-21 — Acme Raises $1B", "snippet a"),
        _mention("2026-05-09 — Jane Promoted to Partner", "snippet b"),
    ]
    client = _client(
        '[{"index":0,"category":"Funding & Deals","summary":"Acme closed a $1B fund.","importance":0.9},'
        '{"index":1,"category":"Leadership Moves","summary":"Jane made partner.","importance":0.7}]'
    )
    curated, ti, to = curate_news(client, "Jane", "Acme", mentions)
    assert ti == 12 and to == 6
    assert curated[0].category == "Funding & Deals" and curated[0].importance == 0.9
    assert curated[0].summary == "Acme closed a $1B fund."
    assert curated[1].category == "Leadership Moves"


def test_client_bad_category_falls_back():
    mentions = [_mention("2026-05-21 — X", "snip")]
    client = _client('[{"index":0,"category":"Sports","summary":"y","importance":2.0}]')
    curated, _, _ = curate_news(client, "Jane", "Acme", mentions)
    assert curated[0].category == "Company News"   # invalid category -> neutral
    assert curated[0].importance == 1.0            # clamped to [0,1]


def test_client_error_falls_back_per_article():
    def boom(**_):
        raise RuntimeError("nope")
    client = SimpleNamespace(messages=SimpleNamespace(create=boom))
    mentions = [_mention("2026-05-21 — X", "snip")]
    curated, ti, to = curate_news(client, "Jane", "Acme", mentions)
    assert ti == 0 and to == 0
    assert curated[0].category == "Company News" and curated[0].summary == "snip"


# --- news_items: pull press-worthy public_links, not just news_mention --------

from news_curate import news_items  # noqa: E402


def _link(value, url):
    return ClaimRow("public_links", value, url, "", 0.8, "perplexity")


def test_news_items_includes_press_worthy_public_links():
    claims = [
        _link("Podcast: Using ETFs in Model Portfolios", "https://www.etf.com/podcasts/x"),
        _link("Fixed Income Overview article", "https://www.sageadvisory.com/article/y"),
    ]
    assert len(news_items(claims)) == 2


def test_news_items_excludes_social_and_directory_links():
    claims = [
        _link("LinkedIn", "https://linkedin.com/in/jane"),
        _link("Twitter", "https://twitter.com/jane"),
        _link("Komson — Partner", "https://theorg.com/org/sage/jane"),     # directory
        _link("Advisor profile", "https://app.getwarmer.com/advisors/x"),  # directory
    ]
    assert news_items(claims) == []


def test_news_items_keeps_news_mention_and_links_together():
    claims = [
        _mention("2026-01-02 — Fund Closes"),
        _link("Interview on markets", "https://www.barrons.com/articles/z"),
        _link("LinkedIn", "https://linkedin.com/in/jane"),  # dropped
    ]
    items = news_items(claims)
    assert len(items) == 2
    assert {c.claim_type for c in items} == {"news_mention", "public_links"}


def test_curate_promotes_public_link_to_feed():
    """A press-worthy public_link with no client still yields a curated row."""
    claims = [_link("Podcast on ETFs", "https://www.etf.com/podcasts/x")]
    curated, _, _ = curate_news(None, "Jane", "Sage", claims)
    assert len(curated) == 1
    assert curated[0].headline == "Podcast on ETFs"
    assert curated[0].source_host == "etf.com"
