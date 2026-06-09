"""Unit tests for the cohort KPI roll-up, focused on the MD fair-shot rule."""
from __future__ import annotations

from insights_store import InsightsSnapshot
from kpi_rollup import (
    advanced_degree_rate,
    avg_tenure,
    avg_years_to_md,
    count_flag,
    kpi_signature_stats,
    landing_sectors,
    left_texas_rate,
    reached_md_stats,
    transitioned_count,
    with_kpi_stats,
)
from person_insights_store import PersonInsight


def _p(pid, *, grad=None, md=False, buy=False, fp=False, sff=False, sss=False,
       adv=False, tenure=None, ytm=None, left=None, sector=""):
    return PersonInsight(
        person_id=pid, grad_year=grad, grad_year_source="class-map",
        first_employer="X", on_buy_side=buy, reached_md=md,
        founder_partner=fp, still_first_firm=sff, started_sell_side=sss,
        has_advanced_degree=adv, tenure_years=tenure, years_to_md=ytm,
        left_texas=left, current_sector=sector,
    )


def test_count_flag():
    people = [_p(1, buy=True), _p(2, buy=True), _p(3, buy=False)]
    assert count_flag(people, "on_buy_side") == 2


def test_md_recent_grad_not_counted_against():
    # Two senior grads (2010): one made MD, one didn't.
    # One recent grad (2022) who hasn't made MD -> excluded from denominator.
    people = [
        _p(1, grad=2010, md=True),
        _p(2, grad=2010, md=False),
        _p(3, grad=2022, md=False),
    ]
    num, den, pct = reached_md_stats(people, snapshot_year=2026)
    assert num == 1 and den == 2  # recent non-MD grad excluded
    assert pct == 50


def test_md_recent_grad_who_made_it_counts_both():
    # Recent grad (2023) who already made MD counts in BOTH num and den.
    people = [
        _p(1, grad=2010, md=True),
        _p(2, grad=2023, md=True),
        _p(3, grad=2024, md=False),  # excluded
    ]
    num, den, pct = reached_md_stats(people, snapshot_year=2026)
    assert num == 2 and den == 2 and pct == 100


def test_md_no_grad_year_only_counts_if_reached():
    people = [
        _p(1, grad=None, md=True),   # counts (reached)
        _p(2, grad=None, md=False),  # excluded (can't prove fair shot)
    ]
    num, den, pct = reached_md_stats(people, snapshot_year=2026)
    assert num == 1 and den == 1 and pct == 100


def test_md_boundary_exactly_ten_years():
    # graduated exactly 10 years before snapshot -> had a fair shot
    people = [_p(1, grad=2016, md=False)]
    num, den, _ = reached_md_stats(people, snapshot_year=2026, md_years=10)
    assert den == 1 and num == 0


def test_signature_stats_shape_and_order():
    people = [
        _p(1, grad=2010, md=True, buy=True, fp=True, sff=False),
        _p(2, grad=2012, md=True, buy=True, fp=False, sff=True),
        _p(3, grad=2024, md=False, buy=False, fp=False, sff=False),
    ]
    stats = kpi_signature_stats(people, snapshot_year=2026)
    labels = [s.label for s in stats]
    # the 4 KPIs lead the scorecard, in order...
    assert labels[:4] == [
        "Now on the buy-side",
        "Reached MD or above",
        "Founders & partners",
        "Still at their first firm",
    ]
    # ...then the folded-in cohort stats follow (degree rate always present)
    assert "Earned a graduate degree" in labels
    # buy-side 2/3 = 67%
    assert stats[0].value == "67%"
    # MD: denom = grads<=2016 (1,2) + reached (none extra) = 2; num = 2 -> 100%
    assert stats[1].value == "100%"
    # founders count = 1 (displayed as a count, not %)
    assert stats[2].value == "1"
    # still-first-firm 1/3 = 33%
    assert stats[3].value == "33%"


def test_signature_stats_empty_when_none_classified():
    assert kpi_signature_stats([], snapshot_year=2026) == ()


def _blank_snap():
    return InsightsSnapshot(
        snapshot_year=2026, people_total=100, enriched_count=3, coverage=0.03,
        is_sample=True, narrative="n", founders_partners=99,
    )


def test_with_kpi_stats_overlays_tiles_and_founders():
    people = [_p(1, grad=2010, md=True, fp=True), _p(2, grad=2012, fp=True)]
    out = with_kpi_stats(_blank_snap(), people, snapshot_year=2026)
    assert [s.label for s in out.signature_stats][0] == "Now on the buy-side"
    assert out.founders_partners == 2  # re-derived from per-person flags


def test_transitioned_count_requires_both():
    people = [
        _p(1, buy=True, sss=True),    # moved in from banking -> counts
        _p(2, buy=True, sss=False),   # buy-side but didn't start sell-side
        _p(3, buy=False, sss=True),   # started sell-side, still there
    ]
    assert transitioned_count(people) == 1


def test_buy_side_detail_shows_transition():
    people = [_p(1, buy=True, sss=True), _p(2, buy=True, sss=True)]
    stats = kpi_signature_stats(people, snapshot_year=2026)
    assert "2 moved in from banking or consulting" == stats[0].detail


def test_secondary_metrics():
    people = [
        _p(1, adv=True, tenure=8, ytm=8, left=True, sector="Private Equity & Credit"),
        _p(2, adv=False, tenure=4, ytm=None, left=False, sector="Private Equity & Credit"),
        _p(3, adv=True, tenure=None, ytm=6, left=None, sector="Investment Banking"),
    ]
    assert advanced_degree_rate(people) == 67          # 2/3
    assert avg_tenure(people) == 6.0                    # (8+4)/2
    assert avg_years_to_md(people) == 7.0               # (8+6)/2
    assert left_texas_rate(people) == (50, 2)          # 1 of 2 known


def test_landing_sectors_sorted_skips_blank():
    people = [
        _p(1, sector="Private Equity & Credit"),
        _p(2, sector="Private Equity & Credit"),
        _p(3, sector="Investment Banking"),
        _p(4, sector=""),  # unenriched — skipped
    ]
    secs = landing_sectors(people)
    assert secs[0].sector == "Private Equity & Credit" and secs[0].count == 2
    assert secs[1].sector == "Investment Banking" and secs[1].count == 1
    assert all(s.sector for s in secs)  # no blank bucket


def test_with_kpi_stats_sets_landing_sectors():
    people = [_p(1, sector="Investment Banking"), _p(2, sector="Investment Banking")]
    out = with_kpi_stats(_blank_snap(), people, snapshot_year=2026)
    assert out.landing_sectors[0].sector == "Investment Banking"
    assert out.landing_sectors[0].count == 2


def test_with_kpi_stats_empty_keeps_founders_clears_tiles():
    out = with_kpi_stats(_blank_snap(), [], snapshot_year=2026)
    assert out.signature_stats == ()
    assert out.founders_partners == 99  # unchanged when nobody classified
