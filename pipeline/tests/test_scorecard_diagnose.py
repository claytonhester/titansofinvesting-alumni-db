"""Tests for the diagnosis layer — deterministic cause->lever map + LLM path.

The map is pure (ScorecardRun -> Diagnosis); the LLM narrative is tested with a
fake Anthropic client (success, usage capture, and a raising client that must
degrade to an empty Narrative).
"""
from __future__ import annotations

from scorecard import CategoryScore, ScorecardRun
from scorecard_diagnose import (
    Diagnosis,
    diagnose,
    llm_narrative,
    render_diagnosis,
)


def _run(categories, *, composite=80, grade="B", gated=False):
    return ScorecardRun(
        timestamp="2026-06-11T00:00:00+00:00", label="t", n=10,
        categories=categories, composite=composite, grade=grade,
        gated=gated, per_person={},
    )


def _cat(name, score, metrics=None, caveat=""):
    return CategoryScore(name, score, metrics or {}, caveat)


# --- wins vs issues ------------------------------------------------------------

def test_strong_category_is_a_win():
    run = _run({"coverage": _cat("coverage", 90)})
    diag = diagnose(run)
    assert any("Coverage" in w for w in diag.wins)
    assert not diag.issues


def test_weak_category_becomes_issue_with_lever():
    run = _run({"coverage": _cat("coverage", 40, {"current_role": 40})})
    diag = diagnose(run)
    assert len(diag.issues) == 1
    item = diag.issues[0]
    assert item.category == "Coverage" and item.lever and item.cause


def test_unmeasured_category_is_skipped():
    run = _run({"accuracy": _cat("accuracy", None)})
    diag = diagnose(run)
    assert not diag.wins and not diag.issues


def test_coherence_issue_names_top_failing_rules():
    cat = _cat("coherence", 92,
               {"by_rule": {"no_zero_duration_dupes": 20, "employer_in_history": 14},
                "p0": 0})
    diag = diagnose(_run({"coherence": cat}))
    assert "no_zero_duration_dupes×20" in diag.issues[0].finding


def test_coherence_p0_is_a_stop_finding():
    cat = _cat("coherence", 80, {"p0": 1, "by_rule": {}})
    diag = diagnose(_run({"coherence": cat}))
    assert "STOP" in diag.issues[0].lever


# --- identity + regression specials --------------------------------------------

def test_identity_violation_is_top_issue():
    run = _run({
        "coverage": _cat("coverage", 95),
        "identity": _cat("identity", 0, {"violations": ["Ghosty: filled"]}),
    })
    diag = diagnose(run)
    assert diag.issues[0].category == "Identity safety"
    assert "Do not ship" in diag.issues[0].lever


def test_identity_clean_is_a_win():
    run = _run({"identity": _cat("identity", 100, {"violations": []})})
    diag = diagnose(run)
    assert any("Identity" in w for w in diag.wins)


def test_regression_drop_is_an_issue():
    run = _run({
        "coverage": _cat("coverage", 95),
        "regression": _cat("regression", 80, {"drop_count": 2, "dropped": [7, 9]}),
    })
    diag = diagnose(run)
    assert any(i.category == "Regression" for i in diag.issues)


# --- rendering -----------------------------------------------------------------

def test_render_has_both_sections():
    diag = Diagnosis(
        wins=("Coverage 90 (≥80)",),
        issues=(diagnose(_run({"coverage": _cat("coverage", 40)})).issues[0],),
    )
    out = render_diagnosis(diag)
    assert "What went well" in out and "What to improve" in out
    assert "cause:" in out and "lever:" in out


# --- LLM narrative -------------------------------------------------------------

class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeUsage:
    input_tokens = 1200
    output_tokens = 180


class _FakeResp:
    content = [_FakeBlock("Healthy batch. Fix 1: corroboration. Fix 2: thin tail.")]
    usage = _FakeUsage()


def _client(raise_it=False):
    """A minimal stand-in for Anthropic: exposes .messages.create()."""
    def create(**kwargs):
        if raise_it:
            raise RuntimeError("model down")
        return _FakeResp()

    client = type("FakeClient", (), {})()
    client.messages = type("M", (), {"create": staticmethod(create)})()
    return client


def test_llm_narrative_returns_text_and_usage():
    nar = llm_narrative({"composite": 82}, Diagnosis(), client=_client(),
                        model="claude-sonnet")
    assert "Fix 1" in nar.text
    assert nar.tokens_in == 1200 and nar.tokens_out == 180


def test_llm_narrative_survives_model_error():
    nar = llm_narrative({"composite": 82}, Diagnosis(), client=_client(raise_it=True),
                        model="claude-sonnet")
    assert nar.text == "" and nar.tokens_in == 0
