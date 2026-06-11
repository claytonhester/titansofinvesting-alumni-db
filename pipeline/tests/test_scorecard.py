"""Pure-factory tests for the scorecard engine + renderer.

No DB, no LLM — every PersonRecord is built by hand. Persistence is tested by
pointing SCORECARD_LOG at a tmp file. Mirrors test_coherence / test_kpi_rollup.
"""
from __future__ import annotations

import json

import scorecard as sc
from enrichment_store import ClaimRow
from scorecard import (
    CategoryScore,
    PersonRecord,
    composite_score,
    coherence_category,
    corroboration_category,
    cost_category,
    coverage_category,
    is_gated,
    letter_grade,
    regression_category,
    richness_category,
)
from scorecard_render import render_table


def _claim(ct, value, method="firecrawl", url="", quote=""):
    return ClaimRow(ct, value, url, quote, 0.9, method)


def _full_person(pid=1, completeness=80, *, future=False, undated=False):
    end = "2030-present" if future else "2018-present"
    career = "Partner at Acme (2018-2020)" if undated is False else "Partner at Acme"
    claims = (
        _claim("current_employer", "Acme"),
        _claim("current_title", "Partner"),
        _claim("education", "BBA from A&M"),
        _claim("short_bio", "A bio."),
        _claim("public_links", "Profile", url="https://linkedin.com/in/x"),
        _claim("career_history", f"Partner at Acme ({end})"),
        _claim("career_history", career),
    )
    return PersonRecord(pid, f"P{pid}", claims, grad_year=2014,
                        completeness=completeness)


def _thin_person(pid=2):
    return PersonRecord(pid, f"P{pid}", (_claim("current_employer", "X"),),
                        grad_year=None, completeness=10)


# --- coverage ------------------------------------------------------------------

def test_coverage_full_person_scores_high():
    cat = coverage_category([_full_person()])
    assert cat.score == 100  # all six presence flags set
    assert cat.metrics["linkedin"] == 100


def test_coverage_thin_person_scores_low_and_caveats():
    cat = coverage_category([_thin_person()])
    # Only current_role present of six -> ~17.
    assert cat.score < sc.LOW_COVERAGE and cat.caveat


# --- richness ------------------------------------------------------------------

def test_richness_is_mean_completeness():
    cat = richness_category([_full_person(completeness=90), _thin_person()])
    assert cat.score == 50  # (90 + 10) / 2
    assert cat.metrics["thin"] == 1


# --- coherence -----------------------------------------------------------------

def test_coherence_clean_batch_is_100():
    cat = coherence_category([_full_person()])
    assert cat.score == 100 and cat.metrics["p0"] == 0


def test_coherence_future_date_flags_p0_and_caveat():
    cat = coherence_category([_full_person(future=True)])
    assert cat.metrics["p0"] == 1 and cat.caveat


# --- corroboration -------------------------------------------------------------

def test_corroboration_counts_reconciled_multi_source():
    claims = (
        _claim("current_employer", "Acme", method="firecrawl+pdl+reconciled"),
        _claim("education", "BBA", method="firecrawl"),  # single source
    )
    rec = PersonRecord(1, "P", claims, grad_year=2014, completeness=50)
    cat = corroboration_category([rec])
    assert cat.metrics["corroborated"] == 1 and cat.metrics["claims"] == 2
    assert cat.score == 50


def test_corroboration_ignores_single_source_plus_method():
    # 'perplexity+haiku-verify' is ONE family despite the '+'.
    claims = (_claim("public_links", "x", method="perplexity+haiku-verify"),)
    rec = PersonRecord(1, "P", claims, grad_year=2014, completeness=50)
    assert corroboration_category([rec]).metrics["corroborated"] == 0


# --- cost ----------------------------------------------------------------------

def test_cost_at_or_below_target_is_100():
    assert cost_category(0.30, 1).score == 100


def test_cost_above_ceiling_is_zero():
    assert cost_category(2.0, 1).score == 0


def test_cost_unmeasured_is_none():
    assert cost_category(None, 5).score is None
    assert cost_category(1.0, 0).score is None


# --- composite + grade ---------------------------------------------------------

def _cat(name, score):
    return CategoryScore(name, score, {})


def test_composite_renormalizes_over_measured():
    cats = {
        "coverage": _cat("coverage", 80),
        "accuracy": CategoryScore("accuracy", None, {}),  # excluded
        "identity": _cat("identity", 60),
        "richness": _cat("richness", 80),
        "coherence": _cat("coherence", 100),
        "corroboration": _cat("corroboration", 20),
        "cost": _cat("cost", 100),
    }
    comp = composite_score(cats)
    # Weighted over present weights (0.20+0.15+0.15+0.15+0.10+0.05 = 0.80).
    expected = round(
        (0.20 * 80 + 0.15 * 60 + 0.15 * 80 + 0.15 * 100 + 0.10 * 20 + 0.05 * 100)
        / 0.80
    )
    assert comp == expected


def test_letter_grade_bands():
    assert letter_grade(95, False) == "A"
    assert letter_grade(85, False) == "B"
    assert letter_grade(72, False) == "C"
    assert letter_grade(61, False) == "D"
    assert letter_grade(40, False) == "F"


def test_hard_gate_caps_grade_to_review():
    assert letter_grade(99, True) == "REVIEW"


def test_is_gated_on_future_date():
    cats = {"coherence": coherence_category([_full_person(future=True)])}
    assert is_gated(cats)
    cats_clean = {"coherence": coherence_category([_full_person()])}
    assert not is_gated(cats_clean)


def test_is_gated_on_gold_identity_violation():
    cats = {
        "coherence": coherence_category([_full_person()]),  # clean
        "identity": CategoryScore("identity", 0,
                                  {"violations": ["X: ghost filled"]}, "GOLD"),
    }
    assert is_gated(cats)


# --- regression ----------------------------------------------------------------

def test_regression_none_on_first_run():
    cat = regression_category([_full_person()], prior=None)
    assert cat.score is None


def test_regression_flags_completeness_drop():
    prior = {"1": {"completeness": 80, "coherent": True}}
    rec = _full_person(pid=1, completeness=50)  # dropped 30
    cat = regression_category([rec], prior=prior)
    assert cat.metrics["drop_count"] == 1 and 1 in cat.metrics["dropped"]
    assert cat.score == 0  # 1 compared, 1 dropped


def test_regression_clean_when_held_or_improved():
    prior = {"1": {"completeness": 80, "coherent": True}}
    rec = _full_person(pid=1, completeness=85)
    cat = regression_category([rec], prior=prior)
    assert cat.metrics["drop_count"] == 0 and cat.score == 100


# --- persistence round-trip ----------------------------------------------------

def test_scorecard_jsonl_round_trip(tmp_path, monkeypatch):
    log = tmp_path / "scorecard.jsonl"
    monkeypatch.setattr(sc, "SCORECARD_LOG", log)
    run = sc.ScorecardRun(
        timestamp="2026-06-11T00:00:00+00:00", label="t", n=1,
        categories={"coverage": _cat("coverage", 77)},
        composite=80, grade="B", gated=False,
        per_person={"1": {"completeness": 80, "coherent": True}},
    )
    sc.append_run(run)
    runs = sc.load_runs()
    assert len(runs) == 1
    assert runs[0]["composite"] == 80
    assert runs[0]["categories"]["coverage"]["score"] == 77
    assert sc.last_run_timestamp() == "2026-06-11T00:00:00+00:00"


# --- render --------------------------------------------------------------------

def test_render_table_marks_current_and_gate():
    run = sc.ScorecardRun(
        timestamp="2026-06-11T00:00:00+00:00", label="batch", n=2,
        categories={
            "coverage": _cat("coverage", 77),
            "accuracy": CategoryScore("accuracy", None, {}, "Phase B"),
            "identity": _cat("identity", 70),
            "richness": _cat("richness", 78),
            "coherence": CategoryScore("coherence", 90, {"p0": 1}, "impossible data (P0)"),
            "corroboration": _cat("corroboration", 16),
            "cost": _cat("cost", 95),
            "regression": CategoryScore("regression", None, {}, "first run"),
        },
        composite=72, grade="REVIEW", gated=True, per_person={},
    )
    table = render_table([], run, history=4)
    assert "Coverage" in table and "**77" in table
    assert "—" in table  # accuracy unmeasured
    assert "HARD GATE TRIPPED" in table
    assert "Composite: 72" in table
