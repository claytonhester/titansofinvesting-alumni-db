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


def _two_phase_client(triage_text, verify_text):
    """A client that returns the triage verdict on the first call and the
    verification verdict on subsequent (per-item) calls."""
    calls = {"n": 0}

    def create(**_):
        calls["n"] += 1
        text = triage_text if calls["n"] == 1 else verify_text
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text=text)],
            usage=SimpleNamespace(input_tokens=10, output_tokens=5),
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_shown_depths_above_threshold_are_kept():
    mentions = [
        _mention("2026-05-21 — Jane on the macro outlook", "snippet a"),
        _mention("2026-05-09 — Jane Promoted to Partner", "snippet b"),
    ]
    client = _client(
        '[{"index":0,"subject_depth":"substantive","category":"Market Views","summary":"Jane shares her macro outlook.","importance":0.8},'
        '{"index":1,"subject_depth":"feature","category":"Leadership Moves","summary":"Jane made partner.","importance":0.7}]'
    )
    curated, ti, to = curate_news(client, "Jane", "Acme", mentions)
    assert ti == 12 and to == 6 and len(curated) == 2
    # The feature outranks the higher-importance substantive item.
    assert curated[0].category == "Leadership Moves"
    assert curated[0].importance == 0.7


def test_feature_outranks_substantive_only_at_equal_strength():
    """Importance still wins across a big gap; depth only breaks near-ties."""
    mentions = [
        _mention("2026-05-21 — Jane quoted briefly with a view", "a"),
        _mention("2026-05-09 — Profile of Jane", "b"),
    ]
    client = _client(
        '[{"index":0,"subject_depth":"substantive","category":"Market Views","summary":"s","importance":0.95},'
        '{"index":1,"subject_depth":"feature","category":"Market Views","summary":"f","importance":0.6}]'
    )
    curated, _, _ = curate_news(client, "Jane", "Acme", mentions)
    # Depth is the PRIMARY sort key, so the feature leads despite lower importance.
    assert curated[0].importance == 0.6


def test_passing_mention_is_dropped():
    """Named in passing with no insight -> not the story -> cut."""
    mentions = [_mention("2026-05-21 — Conference attendee list", "snip")]
    client = _client('[{"index":0,"subject_depth":"passing","category":"Market Views","summary":"x","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_not_about_is_dropped():
    mentions = [_mention("2026-05-21 — Acme launches a product", "snip")]
    client = _client('[{"index":0,"subject_depth":"not_about","category":"Company News","summary":"x","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_unknown_depth_is_dropped():
    """An unrecognized depth label is not in the shown set -> cut."""
    mentions = [_mention("2026-05-21 — X", "snip")]
    client = _client('[{"index":0,"subject_depth":"maybe","category":"Market Views","summary":"x","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_company_news_category_is_excluded_even_if_shown():
    mentions = [_mention("2026-05-21 — Acme opens a London office", "snip")]
    client = _client('[{"index":0,"subject_depth":"feature","category":"Company News","summary":"x","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_below_importance_threshold_dropped():
    mentions = [_mention("2026-05-21 — Jane quoted in passing", "snip")]
    client = _client('[{"index":0,"subject_depth":"substantive","category":"Market Views","summary":"x","importance":0.3}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_invalid_category_dropped():
    mentions = [_mention("2026-05-21 — X", "snip")]
    client = _client('[{"index":0,"subject_depth":"feature","category":"Sports","summary":"y","importance":0.9}]')
    assert curate_news(client, "Jane", "Acme", mentions)[0] == []


def test_public_salary_records_host_is_excluded_from_feed():
    """A public-salary / records database (e.g. texastaxpayers.com) is not editorial
    news: surfacing 'X earned $408,000' as Recognition is a privacy/quality problem.
    Such hosts are dropped even when the model would otherwise keep the item.
    (Live case: Kimberly Carey, 'Highest Paid State Employees 2022'.)"""
    mentions = [
        _mention(
            "2022-01-01 — Highest Paid State Employees 2022",
            "Kimberly Carey earned $408,000",
            url="https://www.texastaxpayers.com/highest-paid-2022",
        )
    ]
    client = _client(
        '[{"index":0,"subject_depth":"substantive","category":"Recognition",'
        '"summary":"Earned $408,000, above median.","importance":0.7}]'
    )
    curated, _, _ = curate_news(client, "Kimberly Carey", "TRS", mentions)
    assert curated == []


def test_model_error_returns_empty():
    def boom(**_):
        raise RuntimeError("nope")
    client = SimpleNamespace(messages=SimpleNamespace(create=boom))
    mentions = [_mention("2026-05-21 — X", "snip")]
    assert curate_news(client, "Jane", "Acme", mentions) == ([], 0, 0)


def test_verify_drops_item_when_article_says_not_about():
    """The triage likes it, but the article shows the person is only name-dropped in
    someone else's award entry -> verification drops it (the Ross Willmann case)."""
    mentions = [_mention("2016-01-01 — 2016 Forty Under Forty", "names Ross in a team blurb")]
    triage = '[{"index":0,"subject_depth":"substantive","category":"Recognition","summary":"Ross named to 40u40","importance":0.7}]'
    verify = '{"subject_depth":"not_about","headline":"2016 Forty Under Forty","category":"Recognition","summary":"x","importance":0.2}'
    client = _two_phase_client(triage, verify)
    curated, _, _ = curate_news(
        client, "Ross Willmann", "Warwick", mentions,
        fetch_article=lambda url: "Chris Halaska, CIO of Memorial Hermann ... would bring Ross Willmann to his team.",
    )
    assert curated == []


def test_verify_corrects_headline_and_summary_from_article():
    """The triage has a generic profile-title headline; the article reveals the real
    honor -> verification rewrites it (the Nicholas Gagnet / Forbes 30 Under 30 case)."""
    mentions = [_mention("2026-01-01 — Nicholas Gagnet - Investor, Coatue", "leads semis at Coatue")]
    triage = '[{"index":0,"subject_depth":"substantive","category":"Recognition","summary":"Nicholas is an investor at Coatue","importance":0.6}]'
    verify = ('{"subject_depth":"substantive","headline":"Forbes 30 Under 30 - Finance (2026)",'
              '"category":"Recognition","summary":"Named to Forbes 30 Under 30 in Finance (2026) for leading Coatue\'s semiconductor investing.","importance":0.85}')
    client = _two_phase_client(triage, verify)
    curated, _, _ = curate_news(
        client, "Nicholas Gagnet", "Coatue", mentions,
        fetch_article=lambda url: "Forbes Lists: 30 Under 30 - Finance (2026). Gagnet helps lead the semiconductors practice at Coatue.",
    )
    assert len(curated) == 1
    assert curated[0].headline == "Forbes 30 Under 30 - Finance (2026)"
    assert "30 Under 30" in curated[0].summary
    assert curated[0].importance == 0.85


def test_verify_drops_item_when_article_fetch_fails():
    """A missing article means the item was never verified against its source — the
    triage judged subject-depth from headline+snippet alone, which is how someone
    else's award got mis-attributed. Precision over recall: drop it (the feed is
    re-curated each run, so a transient scrape failure self-heals)."""
    mentions = [_mention("2026-05-01 — Jane on the macro outlook", "her rates view")]
    triage = '[{"index":0,"subject_depth":"substantive","category":"Market Views","summary":"Jane shares her outlook","importance":0.7}]'
    client = _two_phase_client(triage, "ignored")
    curated, _, _ = curate_news(
        client, "Jane Doe", "Acme", mentions, fetch_article=lambda url: "",
    )
    assert curated == []


def test_verify_keeps_item_when_fetch_succeeds_but_model_call_fails():
    """A successful fetch followed by a transient LLM error is NOT the same as never
    reading the article: we keep the triage verdict conservatively so an LLM 429
    doesn't empty a feed that was about to verify."""
    mentions = [_mention("2026-05-01 — Jane on the macro outlook", "her rates view")]
    triage = '[{"index":0,"subject_depth":"substantive","category":"Market Views","summary":"Jane shares her outlook","importance":0.7}]'

    def create(**_):
        if create.calls == 0:
            create.calls += 1
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text=triage)],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )
        raise RuntimeError("verify LLM 429")
    create.calls = 0
    client = SimpleNamespace(messages=SimpleNamespace(create=create))
    curated, _, _ = curate_news(
        client, "Jane Doe", "Acme", mentions, fetch_article=lambda url: "a real fetched article about Jane",
    )
    assert len(curated) == 1
    assert curated[0].category == "Market Views"


def test_per_person_cap_keeps_top_three():
    mentions = [_mention(f"2026-05-2{i} — Jane item {i}", "s") for i in range(5)]
    verdicts = ",".join(
        f'{{"index":{i},"subject_depth":"substantive","category":"Market Views","summary":"s {i}","importance":{0.6 + i * 0.05}}}'
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


def test_news_items_drops_name_only_bio_titles():
    """A headline that is just the person's name (+ credentials) is a bio page,
    never an article — dropped even on the firm's own content host."""
    claims = [
        _link("Ross Willmann, CFA", "https://www.warwickpartners.net/team/ross"),
        _link("Ross Willmann", "https://www.warwickpartners.net/ross"),
        _mention("2026-01-02 — Ross Willmann", "bio snippet"),           # name-only (dated)
        _link("Ross Willmann Named CIO of the Year", "https://www.barrons.com/x"),  # kept
    ]
    items = news_items(claims, "Ross Willmann")
    # Only the real article (extra words beyond the name) survives.
    assert len(items) == 1
    assert items[0].value == "Ross Willmann Named CIO of the Year"


def test_news_items_without_name_keeps_name_only_titles():
    """The name-only guard only fires when a name is supplied (back-compat)."""
    claims = [_link("Ross Willmann, CFA", "https://www.warwickpartners.net/team/ross")]
    assert len(news_items(claims)) == 1


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
    client = _client('[{"index":0,"subject_depth":"feature","category":"Market Views","summary":"Jane on ETFs","importance":0.8}]')
    curated, _, _ = curate_news(client, "Jane", "Sage", claims)
    assert len(curated) == 1
    assert curated[0].headline == "Podcast on ETFs"
    assert curated[0].source_host == "etf.com"
    assert curated[0].category == "Market Views"
