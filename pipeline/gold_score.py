"""Score a batch against the gold set — the scorecard's answer key.

The deterministic categories (coherence, coverage) judge whether a profile hangs
together; they cannot catch a profile that is internally consistent but WRONG, or
a namesake spliced in cleanly. The gold set is ~20 hand-verified people: for the
positives we know the right employer / title / schools / LinkedIn; for the ghosts
we know the profile must stay empty; for known broker echoes we know a URL must
never be accepted. This module loads that file and scores the batch's gold
members on two axes:

  * Accuracy   — per-field match against the expected values (positives only).
  * Identity   — must_stay_empty ghosts carry no current role, and no
                 must_reject_url leaked into the claims. Any violation is a hard
                 failure that caps the batch grade.

Pure and reference-based: no LLM, no network. Reuses coherence._company_key so an
employer matches across corporate-suffix / acronym variants the same way the
coherence rules see it.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from coherence import _company_key
from config import PIPELINE_DIR
from enrichment_store import ClaimRow

GOLD_PATH = PIPELINE_DIR / "eval" / "gold.json"

# A career entry matches when the company matches and both dated years are within
# this many years of the expected value (sources round/disagree by a year).
_YEAR_SLACK = 1

_REQUIRED_KEYS = {"person_id", "full_name"}


@dataclass(frozen=True)
class GoldRecord:
    person_id: int
    full_name: str
    expect: dict
    must_reject_urls: tuple[str, ...]
    must_stay_empty: bool
    verified_on: str = ""


@dataclass(frozen=True)
class PersonGoldResult:
    person_id: int
    full_name: str
    accuracy: int | None        # None for ghosts (no positive fields to match)
    fields: dict                # per-field pass/fail detail
    violations: tuple[str, ...]  # identity hard-failures for this person


@dataclass(frozen=True)
class GoldReport:
    accuracy: int | None         # mean accuracy over positive gold members in batch
    identity_score: int | None   # 100 minus violations share; None if no golds
    violations: tuple[str, ...]  # all identity hard-failures across the batch
    gold_n: int                  # gold members actually present in the batch
    positives: int               # of those, how many are positives (scored)
    per_person: tuple[PersonGoldResult, ...] = field(default_factory=tuple)

    @property
    def gated(self) -> bool:
        return bool(self.violations)


# --------------------------------------------------------------------------- #
# Loading + validation                                                        #
# --------------------------------------------------------------------------- #

def load_gold(path: Path = GOLD_PATH) -> list[GoldRecord]:
    """Load + validate the gold file. Raises ValueError on a malformed record so
    a typo in the answer key fails loudly rather than silently mis-scoring."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError(f"gold file {path} must be a JSON list")
    records = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict) or not _REQUIRED_KEYS <= item.keys():
            raise ValueError(f"gold record {i} missing {_REQUIRED_KEYS}: {item}")
        records.append(GoldRecord(
            person_id=int(item["person_id"]),
            full_name=str(item["full_name"]),
            expect=item.get("expect") or {},
            must_reject_urls=tuple(item.get("must_reject_urls") or ()),
            must_stay_empty=bool(item.get("must_stay_empty", False)),
            verified_on=str(item.get("verified_on", "")),
        ))
    return records


# --------------------------------------------------------------------------- #
# Field matching                                                              #
# --------------------------------------------------------------------------- #

def _values(claims: list[ClaimRow], claim_type: str) -> list[str]:
    return [c.value for c in claims if c.claim_type == claim_type]


def _norm_text(s: str) -> str:
    return " ".join((s or "").lower().split())


def _employer_matches(expected: str, claims: list[ClaimRow]) -> bool:
    want = _company_key(expected)
    return any(_company_key(v) == want for v in _values(claims, "current_employer"))


def _title_matches(expected: str, claims: list[ClaimRow]) -> bool:
    want = _norm_text(expected)
    return any(want in _norm_text(v) or _norm_text(v) in want
               for v in _values(claims, "current_title"))


def _education_recall(expected: list[str], claims: list[ClaimRow]) -> float:
    if not expected:
        return 1.0
    have = [_norm_text(v) for v in _values(claims, "education")]
    hits = sum(1 for e in expected
               if any(_norm_text(e) in h or h in _norm_text(e) for h in have))
    return hits / len(expected)


def _career_recall(expected: list[dict], claims: list[ClaimRow]) -> float:
    if not expected:
        return 1.0
    from career_analysis import career_entries
    entries = career_entries(claims)
    hits = 0
    for want in expected:
        wc = _company_key(want.get("company", ""))
        ws, we = want.get("start"), want.get("end")
        for e in entries:
            if _company_key(e.company) != wc:
                continue
            if ws is not None and (e.start_year is None
                                   or abs(e.start_year - ws) > _YEAR_SLACK):
                continue
            if we is not None and (e.end_year is None
                                   or abs(e.end_year - we) > _YEAR_SLACK):
                continue
            hits += 1
            break
    return hits / len(expected)


def _has_linkedin(claims: list[ClaimRow]) -> bool:
    return any("linkedin.com/in/" in (c.source_url or "").lower()
               or "linkedin.com/in/" in (c.value or "").lower()
               for c in claims)


# --------------------------------------------------------------------------- #
# Per-person + batch scoring                                                  #
# --------------------------------------------------------------------------- #

def score_person(record: GoldRecord, claims: list[ClaimRow]) -> PersonGoldResult:
    violations: list[str] = []

    # Identity: a ghost must carry no current role.
    if record.must_stay_empty:
        if _values(claims, "current_employer") or _values(claims, "current_title"):
            cur = (_values(claims, "current_employer")
                   or _values(claims, "current_title"))[0]
            violations.append(
                f"{record.full_name}: must_stay_empty but has current role '{cur}'")

    # Identity: no must-reject URL may appear as a claim source.
    sources = {(c.source_url or "").lower() for c in claims}
    for bad in record.must_reject_urls:
        b = bad.lower()
        if any(b in s for s in sources):
            violations.append(f"{record.full_name}: must-reject url leaked '{bad}'")

    if record.must_stay_empty:
        return PersonGoldResult(record.person_id, record.full_name, None, {},
                                tuple(violations))

    # Accuracy: per-field match for positives.
    exp = record.expect
    fields: dict[str, float] = {}
    if "current_employer" in exp:
        fields["employer"] = float(_employer_matches(exp["current_employer"], claims))
    if "current_title" in exp:
        fields["title"] = float(_title_matches(exp["current_title"], claims))
    if "education" in exp:
        fields["education"] = _education_recall(exp["education"], claims)
    if "career" in exp:
        fields["career"] = _career_recall(exp["career"], claims)
    if exp.get("linkedin_url"):
        fields["linkedin"] = float(_has_linkedin(claims))

    accuracy = round(100 * sum(fields.values()) / len(fields)) if fields else None
    return PersonGoldResult(record.person_id, record.full_name, accuracy,
                            {k: round(v, 2) for k, v in fields.items()},
                            tuple(violations))


def score_batch(records: list[GoldRecord],
                claims_by_id: dict[int, list[ClaimRow]]) -> GoldReport:
    """Score only the gold members that appear in this batch (claims_by_id)."""
    present = [r for r in records if r.person_id in claims_by_id]
    if not present:
        return GoldReport(None, None, (), 0, 0, ())

    results = [score_person(r, claims_by_id[r.person_id]) for r in present]
    violations = tuple(v for r in results for v in r.violations)
    accs = [r.accuracy for r in results if r.accuracy is not None]
    accuracy = round(sum(accs) / len(accs)) if accs else None

    # Identity score: full marks minus the share of gold members that violated.
    offenders = len({r.person_id for r in results if r.violations})
    identity_score = round(100 * (len(present) - offenders) / len(present))

    return GoldReport(
        accuracy=accuracy,
        identity_score=identity_score,
        violations=violations,
        gold_n=len(present),
        positives=len(accs),
        per_person=tuple(results),
    )
