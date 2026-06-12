"""Tests for the profile-quality triage bucketing rules."""
from __future__ import annotations

from profile_triage import _bucket


def _b(completeness=90, coherence=100, p0=False, corr=0.5, has_pdl=True, n=10):
    return _bucket(completeness, coherence, p0, corr, has_pdl, n)


def test_solid_is_clean_on_every_axis():
    bucket, reasons = _b()
    assert bucket == "SOLID" and reasons == ()


def test_zero_claims_is_broken():
    bucket, reasons = _b(n=0)
    assert bucket == "BROKEN" and "zero claims" in reasons[0]


def test_future_date_is_broken():
    bucket, reasons = _b(p0=True)
    assert bucket == "BROKEN" and any("impossible" in r for r in reasons)


def test_very_thin_is_broken():
    assert _b(completeness=30)[0] == "BROKEN"


def test_no_pdl_spine_is_at_least_weak():
    bucket, reasons = _b(has_pdl=False)
    assert bucket == "WEAK" and any("no PDL spine" in r for r in reasons)


def test_high_completeness_no_pdl_is_weak_not_solid():
    """Payal/Bart case: looks full (completeness 97) but single-source, no spine."""
    bucket, reasons = _b(completeness=97, corr=0.0, has_pdl=False)
    assert bucket == "WEAK"
    assert any("no PDL spine" in r for r in reasons)


def test_mid_completeness_with_pdl_is_weak_below_60():
    assert _b(completeness=55, has_pdl=True)[0] == "WEAK"


def test_minor_coherence_ding_is_good_not_solid():
    bucket, reasons = _b(coherence=80)
    assert bucket == "GOOD" and any("coherence" in r for r in reasons)


def test_low_corroboration_drops_solid_to_good():
    bucket, reasons = _b(corr=0.1)
    assert bucket == "GOOD" and any("corroboration" in r for r in reasons)


def test_identity_rejects_are_not_a_signal():
    """Rejecting junk sources is the gate working — it must not affect the bucket.
    Two otherwise-identical profiles bucket the same regardless of rejects (the
    function no longer even takes a rejects argument)."""
    assert _b(completeness=90, coherence=100, corr=0.5, has_pdl=True) == ("SOLID", ())
