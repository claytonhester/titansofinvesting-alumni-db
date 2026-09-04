"""--max-usd on the orchestrator + distinct cost-log labels for re-research passes."""
from __future__ import annotations

import pytest

import phase2_enrich
from cost_log import HAIKU_USD_PER_MTOK_IN, PDL_USD_PER_MATCH, PERPLEXITY_USD_PER_REQUEST
from phase2_enrich import batch_label, running_usd
from tests.test_phase2_circuit_breaker import _patch_run_externals, _seed_db


def _usage(haiku_in=0, pdl=0, pplx=0, credits=0):
    return phase2_enrich._PersonUsage(
        credits=credits, haiku_in=haiku_in, haiku_out=0, sonnet_in=0, sonnet_out=0,
        pdl_matches=pdl, pdl_usd=pdl * PDL_USD_PER_MATCH, fc_news_credits=0,
        fc_news_articles=0, perplexity_requests=pplx, sonar_requests=0, sonar_usd=0.0)


# --- running_usd prices every billed source ------------------------------------

@pytest.mark.unit
def test_running_usd_sums_all_sources() -> None:
    usd = running_usd(haiku_in=1_000_000, haiku_out=0, sonnet_in=0, sonnet_out=0,
                      pdl_matches=2, perplexity_requests=10, sonar_requests=1,
                      sonar_usd=0.05, est_credits=0)
    expected = HAIKU_USD_PER_MTOK_IN + 2 * PDL_USD_PER_MATCH \
        + 10 * PERPLEXITY_USD_PER_REQUEST + 0.05
    assert usd == pytest.approx(expected, abs=1e-4)


# --- the cap stops the batch BEFORE the next person, exit code 5 ---------------

def test_max_usd_stops_before_next_person(tmp_path, monkeypatch, capsys):
    db = str(tmp_path / "cap.db")
    _seed_db(db, n=6)
    calls = {"n": 0}

    def _pdl_match_each(*a, **k):
        calls["n"] += 1
        return _usage(pdl=1)  # $0.28 per person

    _patch_run_externals(monkeypatch, db, _pdl_match_each)
    rc = phase2_enrich.run(limit=6, name=None, rerun_enriched=True,
                           max_credits=0, max_usd=0.50)

    assert rc == 5
    assert calls["n"] == 2  # $0.28 < cap, $0.56 >= cap -> stop before the 3rd
    assert "COST CAP $0.50 REACHED" in capsys.readouterr().err


def test_no_cap_by_default(tmp_path, monkeypatch):
    db = str(tmp_path / "nocap.db")
    _seed_db(db, n=4)
    calls = {"n": 0}

    def _spendy(*a, **k):
        calls["n"] += 1
        return _usage(pdl=1)

    _patch_run_externals(monkeypatch, db, _spendy)
    rc = phase2_enrich.run(limit=4, name=None, rerun_enriched=True, max_credits=0)
    assert rc == 0 and calls["n"] == 4


def test_cli_accepts_max_usd():
    args = phase2_enrich.build_parser().parse_args(["--max-usd", "2.5"])
    assert args.max_usd == 2.5
    assert phase2_enrich.build_parser().parse_args([]).max_usd is None


# --- cost-log labels: re-research passes are distinguishable ------------------

@pytest.mark.unit
def test_labels_distinguish_pass_kinds() -> None:
    assert batch_label(7, needs_deep=True) == "deep-7"
    assert batch_label(7, rerun_enriched=True) == "rerun-7"
    assert batch_label(2, ids=[770, 817]) == "ids-2"
    assert batch_label(1, name="Jane Doe") == "Jane Doe"
    assert batch_label(9, titan_class=3, school="Texas A&M") == "Texas A&M-3"
    assert batch_label(9, titan_class=3) == "class-3"
    assert batch_label(5) == "enrich-5"


@pytest.mark.unit
def test_deep_label_wins_over_ids() -> None:
    # --needs-deep is the more specific description of the run than an id list.
    assert batch_label(3, ids=[1], needs_deep=True) == "deep-3"
