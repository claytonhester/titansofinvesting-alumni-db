"""Graduation-year derivation for the per-person insights layer.

The "Reached MD or above" KPI is judged fairly only against people who have had
a fair shot — i.e. graduated at least 10 years ago (see insights_rollup). That
needs a graduation year per person. Two signals, in priority order:

1. ENRICHED EDUCATION — if a degree claim carries a 4-digit year, use it. This is
   the person's actual record. Often absent (our LinkedIn education schema only
   captures degree+school), so it is a bonus when present, not the backbone.
2. TITAN-CLASS MAP — the Titans of Investing class number maps to a calendar year
   per school. This is deterministic and available for everyone, so it is the
   reliable backbone. Anchors (confirmed with the program owner):
     - Texas A&M: founded 2006, ~class 42 by fall 2026  -> ~2.1 classes/year
     - UT Austin: founded Jan 2018, ~class 15 by 2026    -> ~2 classes/year
     - Baylor:    founded Sep 2016, 4 classes on record  -> ~annual (sparse)
   Class numbering is per-school and 0- or 1-based as the data dictates.

When both exist and disagree by more than EDU_CLASS_TOLERANCE years, the class
map wins — a divergent education year is usually a graduate degree, not the
undergrad year the Titans program tracks.

Pure and deterministic — no model, no I/O. Unit-tested directly.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# A class→year line per school: the class number `base_class` happened in
# `base_year`, and the program runs `classes_per_year` cohorts annually. These
# are best-fit estimates from the founding anchors and are intentionally easy to
# retune in one place. Used only to bucket "graduated >= 10 years ago", so ~1yr
# drift is acceptable.
@dataclass(frozen=True)
class ClassMap:
    base_class: int
    base_year: int
    classes_per_year: float


SCHOOL_CLASS_MAPS: dict[str, ClassMap] = {
    "Texas A&M": ClassMap(base_class=0, base_year=2006, classes_per_year=2.1),
    "University of Texas": ClassMap(base_class=1, base_year=2018, classes_per_year=2.0),
    "Baylor": ClassMap(base_class=1, base_year=2016, classes_per_year=1.0),
}

# Plausible bounds for a 4-digit year scraped from a degree string.
_MIN_YEAR = 1970
_MAX_YEAR = 2035
_YEAR_RE = re.compile(r"\b(19[7-9]\d|20[0-3]\d)\b")

# If an education year and the class-map year disagree by more than this, the
# education year is treated as a graduate degree and the class map is trusted.
EDU_CLASS_TOLERANCE = 4


def grad_year_from_class(school: str, titan_class: int | None) -> int | None:
    """Map a (school, class number) onto a calendar graduation year, or None when
    the school is unknown or the class number is missing."""
    if titan_class is None:
        return None
    cm = SCHOOL_CLASS_MAPS.get((school or "").strip())
    if cm is None:
        return None
    year = cm.base_year + round((titan_class - cm.base_class) / cm.classes_per_year)
    if year < _MIN_YEAR or year > _MAX_YEAR:
        return None
    return year


def extract_year(text: str) -> int | None:
    """Earliest plausible 4-digit year in a string (undergrad completion tends to
    be the earliest year on an education line), or None."""
    if not text:
        return None
    years = [int(m) for m in _YEAR_RE.findall(text)]
    plausible = [y for y in years if _MIN_YEAR <= y <= _MAX_YEAR]
    return min(plausible) if plausible else None


def grad_year_from_education(education_texts: list[str]) -> int | None:
    """Earliest plausible year across all education claim values/quotes, or None.
    Earliest because the Titans program is an undergraduate course and undergrad
    completion is the earlier of any degrees on record."""
    years = [extract_year(t) for t in education_texts]
    found = [y for y in years if y is not None]
    return min(found) if found else None


@dataclass(frozen=True)
class GradYear:
    year: int | None
    source: str  # "education" | "class-map" | "" (unknown)


def derive_grad_year(
    school: str,
    titan_class: int | None,
    education_texts: list[str],
) -> GradYear:
    """Combine both signals. Education year wins when present AND consistent with
    the class map (or when there is no class map); otherwise the class map is the
    backbone. Returns the year plus which signal produced it for provenance."""
    edu = grad_year_from_education(education_texts)
    cls = grad_year_from_class(school, titan_class)

    if edu is not None and cls is not None:
        if abs(edu - cls) <= EDU_CLASS_TOLERANCE:
            return GradYear(edu, "education")
        return GradYear(cls, "class-map")
    if edu is not None:
        return GradYear(edu, "education")
    if cls is not None:
        return GradYear(cls, "class-map")
    return GradYear(None, "")
