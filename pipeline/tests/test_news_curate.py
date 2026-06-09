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


def test_no_client_returns_empty():
    """The feed is scarce by design: with no editor judgment, nothing is shown."""
    mentions = [_mention("2026-05-21 — Acme Raises $1B", "Acme closed its fund.")]
    assert curate_news(None, "Jane", "Acme", mentions) == ([], 0, 0)


def _client(text):
    def create(**_):
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=12, output_tokens=6),
        )
    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_show_true_above_threshold_is_kept():
    mentions = [
        _mention("2026-05-21 — Jane on the macro outlook", "snippet a"),
        _mention("2026-05-09 — Jane Promoted to Partner", "snippet b"),
    ]
    client = _client(
        '[{"index":0,"show":true,"category":"Market Views","summary":"Jane shares her macro outlook.","importance":0.8},'
        '{"index":1,"show":true,"category":"Leadership Moves","summary":"Jane made partner.","importance":0.7}]'
    )
    curated, ti, to = curate_news(client, "Jane", "Acme", mentions)
    assert ti == 12 and to == 6 and len(curated) == 2
    assert curated[0].category == "Market Views"          # sorted by importance desc
    assert curated[0].importance == 0.8


def test_show_false_is_dropped():
    mentions = [_mention("2026-05-21 — Acme launches a product", "snip")]
    client = _client('[{"index":0,"show":false,"category":"Company News","summary":"x","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_company_news_category_is_excluded_even_if_shown():
    mentions = [_mention("2026-05-21 — Acme opens a London office", "snip")]
    client = _client('[{"index":0,"show":true,"category":"Company News","summary":"x","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_below_importance_threshold_dropped():
    mentions = [_mention("2026-05-21 — Jane quoted in passing", "snip")]
    client = _client('[{"index":0,"show":true,"category":"Market Views","summary":"x","importance":0.3}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_invalid_category_dropped():
    mentions = [_mention("2026-05-21 — X", "snip")]
    client = _client('[{"index":0,"show":true,"category":"Sports","summary":"y","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_model_error_returns_empty():
    def boom(**_):
        raise RuntimeError("nope")
    client = SimpleNamespace(messages=SimpleNamespace(create=boom))
    mentions = [_mention("2026-05-21 — X", "snip")]
    assert curate_news(client, "Jane", "Acme", mentions) == ([], 0, 0)


def test_per_person_cap_keeps_top_three():
    mentions = [_mention(f"2026-05-2{i} — Jane item {i}", "s") for i in range(5)]
    verdicts = ",".join(
        f'{{"index":{i},"show":true,"category":"Market Views","summary":"s {i}","importance":{0.6 + i * 0.05}}}'
        for i in range(5)
    )
    curated, _, _ = curate_news(_client(f"[{verdicts}]"), "Jane", "Acme", mentions)
    assert len(curated) == 3                       # capped
    assert curated[0].importance >= curated[1].importance >= curated[2].importance


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


def test_news_items_drops_boilerplate_titles():
    """Firm/profile boilerplate is never news, even on a content host."""
    claims = [
        _link("Meet Our Team", "https://www.sageadvisory.com/team"),
        _link("[PDF] Form ADV Part 2B", "https://www.sageadvisory.com/adv.pdf"),
        _link("Company Overview", "https://www.sageadvisory.com/about"),
        _link("Komson on the ETF outlook", "https://www.etf.com/podcasts/x"),  # kept
    ]
    items = news_items(claims)
    assert len(items) == 1
    assert items[0].value == "Komson on the ETF outlook"


def test_curate_promotes_public_link_with_editor_approval():
    """A press-worthy public_link the editor approves (show + importance) is kept."""
    claims = [_link("Podcast on ETFs", "https://www.etf.com/podcasts/x")]
    client = _client('[{"index":0,"show":true,"category":"Market Views","summary":"Jane on ETFs","importance":0.8}]')
    curated, _, _ = curate_news(client, "Jane", "Sage", claims)
    assert len(curated) == 1
    assert curated[0].headline == "Podcast on ETFs"
    assert curated[0].source_host == "etf.com"
    assert curated[0].category == "Market Views"
