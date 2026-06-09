"""Unit tests for grad_year derivation (education parse + class map + combine)."""
from __future__ import annotations

from grad_year import (
    EDU_CLASS_TOLERANCE,
    derive_grad_year,
    extract_year,
    grad_year_from_class,
    grad_year_from_education,
)


def test_class_map_texas_am_anchors():
    # class 0 ~ 2006; the boundary class for a 2026 snapshot's 10-yr rule (2016)
    # should land around class 21.
    assert grad_year_from_class("Texas A&M", 0) == 2006
    assert grad_year_from_class("Texas A&M", 21) == 2016
    assert 2024 <= grad_year_from_class("Texas A&M", 39) <= 2026


def test_class_map_ut_and_baylor():
    assert grad_year_from_class("University of Texas", 1) == 2018
    assert grad_year_from_class("Baylor", 1) == 2016
    assert grad_year_from_class("Baylor", 4) == 2019


def test_class_map_unknown_school_or_missing_class():
    assert grad_year_from_class("Harvard", 3) is None
    assert grad_year_from_class("Texas A&M", None) is None


def test_extract_year_picks_earliest_plausible():
    assert extract_year("BBA 2008, MBA 2014") == 2008
    assert extract_year("Texas A&M University") is None
    assert extract_year("") is None
    # implausible years ignored
    assert extract_year("class of 1850") is None


def test_grad_year_from_education_earliest():
    assert grad_year_from_education(["MBA from Rice (2016)", "BBA 2009"]) == 2009
    assert grad_year_from_education(["BBA from Texas A&M"]) is None
    assert grad_year_from_education([]) is None


def test_derive_prefers_education_when_consistent():
    # education 2015, class map ~2016 — within tolerance -> education wins
    g = derive_grad_year("Texas A&M", 20, ["BBA 2015"])
    assert g.source == "education" and g.year == 2015


def test_derive_falls_back_to_class_when_education_diverges():
    # education 2024 (grad degree), class map ~2016 — beyond tolerance -> class map
    g = derive_grad_year("Texas A&M", 21, ["MBA 2024"])
    assert g.source == "class-map"
    assert abs(g.year - 2016) <= 1


def test_derive_class_map_when_no_education():
    g = derive_grad_year("University of Texas", 1, ["BBA from UT"])
    assert g.source == "class-map" and g.year == 2018


def test_derive_unknown_when_nothing():
    g = derive_grad_year("Harvard", None, [])
    assert g.year is None and g.source == ""


def test_tolerance_constant_sane():
    assert 1 <= EDU_CLASS_TOLERANCE <= 6
