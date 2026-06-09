"""Unit tests for the cohort KPI roll-up, focused on the MD fair-shot rule."""
from __future__ import annotations

from insights_store import InsightsSnapshot
from kpi_rollup import (
    count_flag,
    kpi_signature_stats,
    reached_md_stats,
    with_kpi_stats,
)
from person_insights_store import PersonInsight


def _p(pid, *, grad=None, md=False, buy=False, fp=False, sff=False):
    return PersonInsight(
        person_id=pid, grad_year=grad, grad_year_source="class-map",
        first_employer="X", on_buy_side=buy, reached_md=md,
        founder_partner=fp, still_first_firm=sff,
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
    assert labels == [
        "Now on the buy-side",
        "Reached MD or above",
        "Founders & partners",
        "Still at their first firm",
    ]
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


def test_with_kpi_stats_empty_keeps_founders_clears_tiles():
    out = with_kpi_stats(_blank_snap(), [], snapshot_year=2026)
    assert out.signature_stats == ()
    assert out.founders_partners == 99  # unchanged when nobody classified
