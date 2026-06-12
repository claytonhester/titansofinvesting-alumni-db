"""Quality triage of every already-enriched profile — who is well-rounded and
high-confidence vs who is struggling, so we spend a rerun ONLY where it helps.

Read-only. Spends nothing. Grades each person across five independent signals,
then buckets them so you can see — at a glance — how many profiles are solid and
exactly who needs the work.

The signals (all already in the DB, no new computation of substance):

    completeness   breadth of the profile (compute_completeness, 0-100)
    coherence      internal consistency — no impossible/contradictory data
                   (coherence.py rules; a future date is a hard P0)
    corroboration  share of résumé claims confirmed by 2+ source families
                   (the reconciler's '+reconciled' multi-source tag)
    identity       was the profile built on a clean identity match, or does it
                   carry rejected/queued candidates (namesake risk)?
    spine          does it rest on a PDL résumé spine, or a pre-PDL web scrape?

A profile is only SOLID if it is well-rounded AND well-constructed AND
high-confidence on ALL of these. Anything struggling even a little drops a tier —
that is deliberately a high bar, because the whole point is to find the ones a
rerun would improve.

    python profile_triage.py            # full report
    python profile_triage.py --bucket WEAK   # just one bucket's roster
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from coherence import coherence_report
from compute_completeness import compute_breakdown
from config import DB_PATH
from db import connect
from enrichment_store import ClaimRow
from scorecard import _is_corroborated

NOW_YEAR = 2026

# Résumé claim types corroboration is meaningful for (skills/links rarely repeat
# across sources, so counting them would understate corroboration).
_CORE_TYPES = frozenset({
    "current_employer", "current_title", "career_history", "education",
})

# Bucket thresholds. SOLID is intentionally demanding — well-rounded on every axis.
SOLID_COMPLETENESS = 75
GOOD_COMPLETENESS = 60
WEAK_COMPLETENESS = 40
MIN_SOLID_CORROBORATION = 0.20   # at least a fifth of core claims multi-sourced


@dataclass(frozen=True)
class ProfileGrade:
    person_id: int
    full_name: str
    completeness: int
    coherence: int
    coherence_p0: bool
    corroboration: float          # 0.0-1.0 over core claims
    has_pdl_spine: bool
    identity_rejects: int
    identity_reviews: int
    mean_confidence: float
    n_claims: int
    bucket: str                   # SOLID | GOOD | WEAK | BROKEN
    reasons: tuple[str, ...]      # what pulled it below SOLID


def _load_claims(conn, person_id: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ?", (person_id,),
    ).fetchall()
    return [ClaimRow(r["claim_type"], r["value"], r["source_url"], r["quote"],
                     r["confidence"], r["extraction_method"]) for r in rows]


def _corroboration(claims: list[ClaimRow]) -> float:
    core = [c for c in claims if c.claim_type in _CORE_TYPES]
    if not core:
        return 0.0
    return sum(1 for c in core if _is_corroborated(c)) / len(core)


def _identity_counts(conn, person_id: int) -> tuple[int, int]:
    rows = conn.execute(
        "SELECT decision FROM identity_candidates WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    rej = sum(1 for r in rows if r["decision"] in ("reject", "rejected"))
    rev = sum(1 for r in rows if r["decision"] == "review")
    return rej, rev


def _bucket(completeness, coherence, coherence_p0, corroboration, has_pdl,
            n_claims) -> tuple[str, tuple[str, ...]]:
    """Assign a quality bucket and the reasons it isn't SOLID. Four sound signals
    only — completeness, coherence, corroboration, PDL spine. (Identity-candidate
    REJECTS are deliberately NOT a signal: rejecting a namesake/junk source is the
    gate working, not a flaw; a leak would be an ACCEPTED bad source, which shows
    up as a coherence/corroboration problem instead.)"""
    reasons: list[str] = []
    if n_claims == 0:
        return "BROKEN", ("zero claims",)
    if coherence_p0:
        reasons.append("impossible data (future date)")
    if not has_pdl:
        reasons.append("no PDL spine (pre-PDL/web-only)")
    if completeness < SOLID_COMPLETENESS:
        reasons.append(f"completeness {completeness}<{SOLID_COMPLETENESS}")
    if coherence < 100:
        reasons.append(f"coherence {coherence}<100")
    if corroboration < MIN_SOLID_CORROBORATION:
        reasons.append(f"corroboration {corroboration:.0%}<{MIN_SOLID_CORROBORATION:.0%}")

    # BROKEN: unusable or dangerous — must rerun.
    if completeness < WEAK_COMPLETENESS or coherence_p0:
        return "BROKEN", tuple(reasons)
    # WEAK: clearly thin or built without a PDL spine — should rerun.
    if completeness < GOOD_COMPLETENESS or not has_pdl:
        return "WEAK", tuple(reasons)
    # SOLID: clean on every axis.
    if not reasons:
        return "SOLID", ()
    # GOOD: minor gaps — struggling a little; rerun is optional/low-yield.
    return "GOOD", tuple(reasons)


def grade_person(conn, person_id: int, full_name: str, grad_year: int | None) -> ProfileGrade:
    claims = _load_claims(conn, person_id)
    breakdown = compute_breakdown(claims)
    coh = coherence_report(claims, grad_year, NOW_YEAR)
    corr = _corroboration(claims)
    has_pdl = any("pdl" in (c.extraction_method or "").lower() for c in claims)
    rej, rev = _identity_counts(conn, person_id)
    mean_conf = (sum(c.confidence for c in claims) / len(claims)) if claims else 0.0
    bucket, reasons = _bucket(breakdown.score, coh.score, coh.p0, corr, has_pdl,
                              len(claims))
    return ProfileGrade(
        person_id=person_id, full_name=full_name, completeness=breakdown.score,
        coherence=coh.score, coherence_p0=coh.p0, corroboration=corr,
        has_pdl_spine=has_pdl, identity_rejects=rej, identity_reviews=rev,
        mean_confidence=mean_conf, n_claims=len(claims), bucket=bucket, reasons=reasons,
    )


def grade_all(conn) -> list[ProfileGrade]:
    rows = conn.execute(
        "SELECT pi.person_id, p.full_name, pi.grad_year "
        "FROM person_insights pi JOIN people p ON p.id = pi.person_id "
        "ORDER BY pi.person_id"
    ).fetchall()
    return [grade_person(conn, r["person_id"], r["full_name"], r["grad_year"])
            for r in rows]


_BUCKET_ORDER = ["SOLID", "GOOD", "WEAK", "BROKEN"]
_USD_PER_PERSON = 0.40  # base-sweep rerun, from the canary


def render(grades: list[ProfileGrade], only_bucket: str | None) -> None:
    by_bucket: dict[str, list[ProfileGrade]] = {b: [] for b in _BUCKET_ORDER}
    for g in grades:
        by_bucket[g.bucket].append(g)

    print(f"\n=== PROFILE QUALITY TRIAGE — {len(grades)} enriched profiles ===\n")
    print(f"{'bucket':<8} {'count':>5}   meaning")
    print(f"{'-'*8} {'-'*5}   {'-'*48}")
    meanings = {
        "SOLID": "well-rounded, consistent, corroborated — leave it",
        "GOOD": "minor gaps — rerun optional, low yield",
        "WEAK": "thin or no PDL spine — rerun recommended",
        "BROKEN": "unusable/impossible/zero — must rerun",
    }
    for b in _BUCKET_ORDER:
        print(f"{b:<8} {len(by_bucket[b]):>5}   {meanings[b]}")
    rerun = by_bucket["WEAK"] + by_bucket["BROKEN"]
    rerun_plus = rerun + by_bucket["GOOD"]
    print(f"\nRerun set (WEAK+BROKEN): {len(rerun)} people  ≈ ${len(rerun)*_USD_PER_PERSON:,.2f}")
    print(f"  + GOOD (everything struggling at all): {len(rerun_plus)} people  "
          f"≈ ${len(rerun_plus)*_USD_PER_PERSON:,.2f}")
    print(f"  SOLID (leave alone): {len(by_bucket['SOLID'])} people  — $0\n")

    buckets_to_show = [only_bucket] if only_bucket else _BUCKET_ORDER
    for b in buckets_to_show:
        people = by_bucket.get(b, [])
        if not people:
            continue
        print(f"\n--- {b} ({len(people)}) ---")
        for g in sorted(people, key=lambda x: (x.completeness, x.person_id)):
            flags = "; ".join(g.reasons) if g.reasons else "clean"
            print(f"  [{g.person_id:>4}] cmp{g.completeness:>3} coh{g.coherence:>3} "
                  f"corr{g.corroboration:>4.0%} pdl{'Y' if g.has_pdl_spine else 'N'} "
                  f"cf{g.mean_confidence:>4.2f}  {g.full_name:<24} {flags}")
    print()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bucket", choices=_BUCKET_ORDER, default=None,
                    help="show the roster for just one bucket")
    ap.add_argument("--rerun-ids", action="store_true",
                    help="print ONLY the comma-separated ids of the rerun set "
                         "(WEAK+BROKEN), for piping into phase2_enrich --ids")
    ap.add_argument("--include-good", action="store_true",
                    help="with --rerun-ids, also include GOOD (everything "
                         "struggling at all)")
    args = ap.parse_args(argv)
    with connect(Path(DB_PATH)) as conn:
        grades = grade_all(conn)
    if args.rerun_ids:
        target = {"WEAK", "BROKEN"} | ({"GOOD"} if args.include_good else set())
        ids = [str(g.person_id) for g in grades if g.bucket in target]
        print(",".join(ids))
        return 0
    render(grades, args.bucket)
    return 0


if __name__ == "__main__":
    sys.exit(main())
