"""Unit tests for qa_audit's deterministic logic (metrics / parse / severity)."""
from __future__ import annotations

from enrichment_store import ClaimRow
from qa_audit import AuditResult, _parse_audit, compute_metrics


def _c(ct, value, method="pdl", url=""):
    return ClaimRow(claim_type=ct, value=value, source_url=url, quote="",
                    confidence=0.8, extraction_method=method)


def test_compute_metrics_source_families_and_merges():
    claims = [
        _c("career_history", "A", "pdl"),
        _c("career_history", "B", "claude-haiku-4-5-20251001"),          # firecrawl
        _c("career_history", "C", "firecrawl+pdl+reconciled"),           # both
        _c("public_links", "LinkedIn", "firecrawl-linkedin", "https://linkedin.com/in/x"),
        _c("public_links", "Bio", "perplexity+haiku-verify", "https://x.com"),
    ]
    m = compute_metrics(claims)
    # merged 'firecrawl+pdl' counts toward BOTH firecrawl and pdl
    assert m["by_source"]["pdl"] == 2          # pure pdl + merged
    assert m["by_source"]["firecrawl"] == 2    # pure fc + merged
    assert m["by_source"]["firecrawl_linkedin"] == 1
    assert m["by_source"]["perplexity"] == 1
    # 'perplexity+haiku-verify' is ONE method, not a merge — no phantom family
    assert "haiku-verify" not in m["by_source"]
    assert m["career_count"] == 3
    assert m["mention_count"] == 2
    assert m["has_linkedin"] is True


def test_compute_metrics_coverage_flags():
    claims = [_c("current_employer", "Acme", "pdl"), _c("current_title", "CEO", "pdl"),
              _c("short_bio", "A bio", "claude-haiku-4-5-synthesis")]
    m = compute_metrics(claims)
    assert m["has_current_employer"] and m["has_current_title"] and m["has_bio"]
    assert m["pdl_present"] is True
    # the synthesized bio must NOT count as a Firecrawl collector source
    assert m["firecrawl_present"] is False


def test_parse_audit_clean_and_fenced():
    raw = '{"scores":{"identity":5},"issues":[],"summary":"ok"}'
    assert _parse_audit(raw)["summary"] == "ok"
    fenced = '```json\n{"scores":{},"issues":[],"summary":"x"}\n```'
    assert _parse_audit(fenced)["summary"] == "x"


def test_parse_audit_garbage_returns_none():
    assert _parse_audit("not json") is None


def test_audit_result_counts_by_severity():
    res = AuditResult("Jane", {}, {}, [
        {"severity": "P0"}, {"severity": "P1"}, {"severity": "P1"}, {"severity": "P2"},
    ], "s")
    assert res.counts == {"P0": 1, "P1": 2, "P2": 1}
