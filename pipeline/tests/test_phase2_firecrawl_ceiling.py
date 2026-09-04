"""Run-start Firecrawl ceiling: an unknown live balance must not crash the run."""
from __future__ import annotations

import pytest

import phase2_enrich
from phase2_enrich import resolve_firecrawl_ceiling
from tests.test_phase2_circuit_breaker import _patch_run_externals, _seed_db


@pytest.mark.unit
def test_ceiling_defaults_to_live_balance() -> None:
    assert resolve_firecrawl_ceiling(500, None) == 500


@pytest.mark.unit
def test_ceiling_capped_by_max_credits() -> None:
    assert resolve_firecrawl_ceiling(500, 100) == 100
    assert resolve_firecrawl_ceiling(50, 100) == 50  # can't spend what we don't have


@pytest.mark.unit
def test_unknown_balance_is_zero_even_with_max_credits() -> None:
    # Regression: `min(max_credits, None)` raised TypeError and killed the run
    # before the first person whenever the credit meter errored.
    assert resolve_firecrawl_ceiling(None, None) == 0
    assert resolve_firecrawl_ceiling(None, 100) == 0


@pytest.mark.unit
def test_negative_balance_clamped() -> None:
    assert resolve_firecrawl_ceiling(-5, None) == 0


def test_run_survives_meter_error_and_warns(tmp_path, monkeypatch, capsys):
    """End-to-end through run(): remaining_credits() -> None must yield a
    Firecrawl-free run with a visible warning, not a TypeError."""
    db = str(tmp_path / "meter.db")
    _seed_db(db, n=2)
    usage = phase2_enrich._PersonUsage(
        credits=0, haiku_in=0, haiku_out=0, sonnet_in=0, sonnet_out=0,
        pdl_matches=0, pdl_usd=0.0, fc_news_credits=0, fc_news_articles=0,
        perplexity_requests=0, sonar_requests=0, sonar_usd=0.0)
    seen: dict = {}

    def _capture(*a, **k):
        seen["fc_remaining"] = k["fc_budget"].remaining
        return usage

    _patch_run_externals(monkeypatch, db, _capture)
    monkeypatch.setattr(phase2_enrich, "remaining_credits", lambda fc: None)
    rc = phase2_enrich.run(limit=2, name=None, rerun_enriched=True, max_credits=100)

    assert rc == 0
    assert seen["fc_remaining"] == 0  # Firecrawl-free
    assert "Firecrawl balance unknown" in capsys.readouterr().err
