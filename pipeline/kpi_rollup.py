"""Cohort roll-up of the four headline KPIs from per-person classifications.

Reads the `person_insights` rows (one per classified alumnus) and folds them into
the four scorecard tiles the Overview page headlines:

    Now on the buy-side      share in an investing seat now
    Reached MD or above      share who cleared the senior bar — FAIR-SHOT adjusted
    Founders & partners      headcount running a fund / holding a partner seat
    Still at their first firm share still at their first post-grad employer

The one subtlety is the "Reached MD or above" denominator. A Titan who graduated
three years ago has not had time to make MD, so counting them as a "no" would
unfairly drag the rate down. So the rate is measured only over people who have
had a FAIR SHOT — defined as: they already reached MD+ (counts no matter when),
OR they graduated at least MD_FAIR_SHOT_YEARS years before the snapshot year.
Recent grads who have not yet reached it are excluded from BOTH numerator and
denominator; recent grads who reached it early are included in both.

Pure and deterministic; unit-tested directly. SignatureStat shape is owned by
insights_store so web and pipeline agree on the tile contract.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace

from collections import Counter

from insights_store import InsightsSnapshot, SectorCount, SignatureStat
from person_insights_store import PersonInsight

MD_FAIR_SHOT_YEARS = 10


def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def _avg(values: Sequence[int]) -> float | None:
    return sum(values) / len(values) if values else None


def advanced_degree_rate(insights: Sequence[PersonInsight]) -> int:
    return _pct(count_flag(insights, "has_advanced_degree"), len(insights))


def avg_tenure(insights: Sequence[PersonInsight]) -> float | None:
    return _avg([p.tenure_years for p in insights if p.tenure_years is not None])


def avg_years_to_md(insights: Sequence[PersonInsight]) -> float | None:
    return _avg([p.years_to_md for p in insights if p.years_to_md is not None])


def left_texas_rate(insights: Sequence[PersonInsight]) -> tuple[int, int]:
    """(pct who left Texas, n with a known current location). The denominator is
    only people whose current location we actually know."""
    known = [p for p in insights if p.left_texas is not None]
    left = sum(1 for p in known if p.left_texas)
    return _pct(left, len(known)), len(known)


def landing_sectors(insights: Sequence[PersonInsight]) -> tuple[SectorCount, ...]:
    """Sector breakdown of CURRENT employers (where they land), most common first.
    Blank sectors are skipped so unenriched rows don't pollute the chart."""
    tally: Counter = Counter()
    for p in insights:
        if p.current_sector:
            tally[p.current_sector] += 1
    return tuple(
        SectorCount(sector=s, count=n)
        for s, n in sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    )


def count_flag(insights: Sequence[PersonInsight], attr: str) -> int:
    """How many classified people have a given boolean flag set."""
    return sum(1 for p in insights if getattr(p, attr))


def transitioned_count(insights: Sequence[PersonInsight]) -> int:
    """People who made the classic move: started in banking/consulting/accounting
    AND are on the buy-side now. The measured form of "moved from a bank or
    consultancy into investing"."""
    return sum(1 for p in insights if p.started_sell_side and p.on_buy_side)


def reached_md_stats(
    insights: Sequence[PersonInsight],
    snapshot_year: int,
    *,
    md_years: int = MD_FAIR_SHOT_YEARS,
) -> tuple[int, int, int]:
    """(numerator, denominator, pct) for "Reached MD or above" under the fair-shot
    rule. numerator = reached MD+ ever. denominator = reached MD+ OR graduated at
    least `md_years` years before the snapshot. A person with no grad_year counts
    toward the denominator ONLY if they already reached MD+ (we can't prove they
    had a fair shot otherwise)."""
    cutoff = snapshot_year - md_years
    numerator = 0
    denominator = 0
    for p in insights:
        had_fair_shot = p.reached_md or (p.grad_year is not None and p.grad_year <= cutoff)
        if had_fair_shot:
            denominator += 1
            if p.reached_md:
                numerator += 1
    return numerator, denominator, _pct(numerator, denominator)


def kpi_signature_stats(
    insights: Sequence[PersonInsight],
    *,
    snapshot_year: int,
    md_years: int = MD_FAIR_SHOT_YEARS,
) -> tuple[SignatureStat, ...]:
    """The four headline tiles, in display order, measured from the per-person
    classifications. Returns an empty tuple when no one is classified yet so the
    web shows an empty state rather than a row of zeros."""
    classified = len(insights)
    if classified == 0:
        return ()

    buy = count_flag(insights, "on_buy_side")
    founders = count_flag(insights, "founder_partner")
    still = count_flag(insights, "still_first_firm")
    moved = transitioned_count(insights)
    md_num, md_den, md_pct = reached_md_stats(insights, snapshot_year, md_years=md_years)

    buy_detail = (
        f"{moved} moved in from banking or consulting"
        if moved
        else "moved into an investing seat"
    )
    return (
        SignatureStat(
            label="Now on the buy-side",
            value=f"{_pct(buy, classified)}%",
            detail=buy_detail,
            pct=_pct(buy, classified),
        ),
        SignatureStat(
            label="Reached MD or above",
            value=f"{md_pct}%",
            detail=f"of those already there or {md_years}+ years out",
            pct=md_pct,
        ),
        SignatureStat(
            label="Founders & partners",
            value=str(founders),
            detail="running their own fund or holding a partner seat",
            pct=_pct(founders, classified),
        ),
        SignatureStat(
            label="Still at their first firm",
            value=f"{_pct(still, classified)}%",
            detail="stayed and climbed where they started",
            pct=_pct(still, classified),
        ),
    ) + _secondary_stats(insights)


def _secondary_stats(insights: Sequence[PersonInsight]) -> tuple[SignatureStat, ...]:
    """The folded-in cohort stats that share the scorecard with the 4 KPIs.
    Average tiles carry pct=0 (the view hides the bar); rate tiles carry their %.
    A tile is omitted when its underlying data is entirely missing."""
    out: list[SignatureStat] = []

    deg = advanced_degree_rate(insights)
    out.append(SignatureStat(
        label="Earned a graduate degree",
        value=f"{deg}%",
        detail="went back for an MBA, JD, or other advanced degree",
        pct=deg,
    ))

    ytm = avg_years_to_md(insights)
    if ytm is not None:
        out.append(SignatureStat(
            label="Avg years to MD",
            value=f"{ytm:.0f} yrs",
            detail="from graduation to Managing Director or above",
            pct=0,
        ))

    ten = avg_tenure(insights)
    if ten is not None:
        out.append(SignatureStat(
            label="Avg tenure, current firm",
            value=f"{ten:.0f} yrs",
            detail="years in their current role",
            pct=0,
        ))

    left_pct, known = left_texas_rate(insights)
    if known:
        out.append(SignatureStat(
            label="Left Texas",
            value=f"{left_pct}%",
            detail="live outside Texas now",
            pct=left_pct,
        ))

    return tuple(out)


def with_kpi_stats(
    snap: InsightsSnapshot,
    insights: Sequence[PersonInsight],
    *,
    snapshot_year: int,
    md_years: int = MD_FAIR_SHOT_YEARS,
) -> InsightsSnapshot:
    """Overlay the four per-person KPIs onto a deterministic snapshot, making them
    THE scorecard tiles. founders_partners is re-derived from the per-person
    classification so the headline agrees with the tile. When nobody is classified
    yet the tiles are emptied so the web renders an empty state instead of zeros."""
    stats = kpi_signature_stats(insights, snapshot_year=snapshot_year, md_years=md_years)
    founders = count_flag(insights, "founder_partner") if insights else snap.founders_partners
    return replace(
        snap,
        signature_stats=stats,
        founders_partners=founders,
        landing_sectors=landing_sectors(insights),
    )
