"""Batch scorecard — the model-card treatment for a chunk of enrichment.

After a run of N people we score the batch across categories, compare to prior
batches (trend), and persist one row to data/scorecard.jsonl so the next run can
diff against it. Phase A is fully deterministic (no LLM, no answer key): Coverage,
Richness, Coherence, Source-corroboration, Cost-efficiency, Identity-mix (soft),
and Regression-vs-prior. Accuracy and the hard Identity-safety check arrive with
the gold set (Phase B) — until then those rows render as "—".

Usage:
    python scorecard.py                 # people enriched since the last scorecard
    python scorecard.py --all           # the whole cohort
    python scorecard.py --recent 25     # the 25 most recently enriched
    python scorecard.py --ids 770,16    # an explicit set
    python scorecard.py --since 2026-06-01T00:00:00+00:00
    python scorecard.py --no-save       # don't append to scorecard.jsonl
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from career_analysis import career_entries
from coherence import coherence_report
from config import DATA_DIR, DB_PATH
from db import connect
from enrichment_store import ClaimRow
from gold_score import GoldReport, load_gold
from gold_score import score_batch as score_gold
from person_insights_store import PersonInsight, get_person_insight
from reconcile import RECONCILE_METHOD_SUFFIX, _source_family
from scorecard_render import render_table

SCORECARD_LOG = DATA_DIR / "scorecard.jsonl"


def current_year() -> int:
    return datetime.now(timezone.utc).year

# Composite weights. Categories not measured this run (None score) drop out and
# the rest are renormalized, so a Phase-A run weights only what it can see.
WEIGHTS: dict[str, float] = {
    "coverage": 0.20,
    "accuracy": 0.20,
    "identity": 0.15,
    "richness": 0.15,
    "coherence": 0.15,
    "corroboration": 0.10,
    "cost": 0.05,
}

# Cost-efficiency scoring band: $/verified-profile. At or below TARGET -> 100;
# at or above CEILING -> 0; linear between. Tuned to the measured ~$0.37/profile.
COST_TARGET_USD = 0.40
COST_CEILING_USD = 1.20

# Richness: a completeness_score below this counts as a "thin" profile.
THIN_COMPLETENESS = 40

LOW_COVERAGE = 60  # coverage/richness rows below this get a caveat asterisk


@dataclass(frozen=True)
class CategoryScore:
    """One row of the card. score is None when the category can't be measured
    yet (e.g. Accuracy before a gold set exists) — it renders as '—' and is
    excluded from the composite."""

    name: str
    score: int | None
    metrics: dict
    caveat: str = ""


@dataclass(frozen=True)
class PersonRecord:
    """Everything a person contributes to the batch score, loaded once."""

    person_id: int
    full_name: str
    claims: tuple[ClaimRow, ...]
    grad_year: int | None
    completeness: int


@dataclass
class ScorecardRun:
    timestamp: str
    label: str
    n: int
    categories: dict[str, CategoryScore]
    composite: int
    grade: str
    gated: bool
    per_person: dict[str, dict] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Category computation (pure functions over the loaded records)               #
# --------------------------------------------------------------------------- #

def _pct(part: int, whole: int) -> int:
    return round(100 * part / whole) if whole else 0


def _is_corroborated(claim: ClaimRow) -> bool:
    """A claim is corroborated when its extraction_method records 2+ distinct
    source families via the reconciler's '+reconciled' tag."""
    method = claim.extraction_method or ""
    if not method.endswith(RECONCILE_METHOD_SUFFIX):
        return False
    prefix = method[: -len(RECONCILE_METHOD_SUFFIX)]
    fams = {_source_family(p) for p in prefix.split("+") if p}
    return len(fams) >= 2


def _has_type(claims: tuple[ClaimRow, ...], claim_type: str) -> bool:
    return any(c.claim_type == claim_type for c in claims)


def _has_linkedin(claims: tuple[ClaimRow, ...]) -> bool:
    return any(
        c.claim_type == "public_links" and "linkedin" in (c.source_url or "").lower()
        for c in claims
    )


def coverage_category(records: list[PersonRecord]) -> CategoryScore:
    """Mean of six presence rates — the breadth of what we filled in. Inline
    presence checks keep this free of the firecrawl/Anthropic import chain."""
    n = len(records)
    flags = {
        "current_role": 0, "dated_career": 0, "education": 0,
        "bio": 0, "news": 0, "linkedin": 0,
    }
    for r in records:
        if _has_type(r.claims, "current_employer"):
            flags["current_role"] += 1
        if _has_dated_career(r.claims):
            flags["dated_career"] += 1
        if _has_type(r.claims, "education"):
            flags["education"] += 1
        if _has_type(r.claims, "short_bio"):
            flags["bio"] += 1
        if _has_type(r.claims, "public_links"):
            flags["news"] += 1
        if _has_linkedin(r.claims):
            flags["linkedin"] += 1
    rates = {k: _pct(v, n) for k, v in flags.items()}
    score = round(sum(rates.values()) / len(rates)) if rates else 0
    caveat = "below target" if score < LOW_COVERAGE else ""
    return CategoryScore("coverage", score, rates, caveat)


def _has_dated_career(claims: tuple[ClaimRow, ...]) -> bool:
    return any(
        e.start_year is not None or e.end_year is not None
        for e in career_entries(list(claims))
    )


def richness_category(records: list[PersonRecord]) -> CategoryScore:
    """Distribution of the deterministic completeness_score (0-100)."""
    n = len(records)
    scores = [r.completeness for r in records]
    mean = round(sum(scores) / n) if n else 0
    thin = sum(1 for s in scores if s < THIN_COMPLETENESS)
    caveat = "thin tail" if thin > n * 0.25 else ""
    return CategoryScore(
        "richness", mean,
        {"mean": mean, "thin": thin, "thin_pct": _pct(thin, n)}, caveat,
    )


def coherence_category(records: list[PersonRecord]) -> CategoryScore:
    """Mean per-person coherence; any future-date P0 sets the hard-gate caveat.
    Records a per-rule failure breakdown so diagnosis can name the lever."""
    n = len(records)
    now = current_year()
    per = [coherence_report(list(r.claims), r.grad_year, now) for r in records]
    mean = round(sum(p.score for p in per) / n) if n else 100
    clean = sum(1 for p in per if not p.failures)
    p0 = sum(1 for p in per if p.p0)
    by_rule: dict[str, int] = {}
    for p in per:
        for name, _ in p.failures:
            by_rule[name] = by_rule.get(name, 0) + 1
    caveat = "impossible data (P0)" if p0 else ""
    return CategoryScore(
        "coherence", mean,
        {"clean": clean, "clean_pct": _pct(clean, n), "p0": p0, "by_rule": by_rule},
        caveat,
    )


def corroboration_category(records: list[PersonRecord]) -> CategoryScore:
    """Share of all claims confirmed by 2+ source families."""
    total = corrob = 0
    for r in records:
        for c in r.claims:
            total += 1
            if _is_corroborated(c):
                corrob += 1
    score = _pct(corrob, total)
    return CategoryScore(
        "corroboration", score, {"corroborated": corrob, "claims": total},
    )


def cost_category(cost_usd: float | None, n_verified: int) -> CategoryScore:
    """$/verified-profile mapped onto the cost band. None when no cost rows
    matched this batch (older batches predate cost logging)."""
    if cost_usd is None or n_verified <= 0:
        return CategoryScore("cost", None, {"reason": "no cost rows matched"},
                             "unmeasured")
    per = cost_usd / n_verified
    if per <= COST_TARGET_USD:
        score = 100
    elif per >= COST_CEILING_USD:
        score = 0
    else:
        span = COST_CEILING_USD - COST_TARGET_USD
        score = round(100 * (COST_CEILING_USD - per) / span)
    return CategoryScore(
        "cost", score,
        {"usd_per_verified": round(per, 3), "batch_usd": round(cost_usd, 2),
         "verified": n_verified},
    )


def _verdict_mix(conn, person_ids: list[int]) -> dict:
    """The identity_candidates verdict mix for this batch's people, with the two
    writers' labels (broker: auto_accept/reject/review; LinkedIn verifier:
    verified/rejected/review) folded together."""
    if not person_ids:
        return {}
    ph = ",".join("?" * len(person_ids))
    rows = conn.execute(
        f"SELECT decision, COUNT(*) c FROM identity_candidates "
        f"WHERE person_id IN ({ph}) GROUP BY decision", person_ids,
    ).fetchall()
    mix = {r["decision"]: r["c"] for r in rows}
    return {
        "verified": sum(v for k, v in mix.items() if k in ("verified", "auto_accept")),
        "review": mix.get("review", 0),
        "rejected": sum(v for k, v in mix.items() if k in ("rejected", "reject")),
        "evaluated": sum(mix.values()),
    }


def identity_category(conn, person_ids: list[int], gold: GoldReport) -> CategoryScore:
    """Identity correctness. The SCORE is the gold hard-check: did every ghost
    stay empty and did no must-reject URL leak (the real, falsifiable measure).
    The verdict MIX rides along as context — a high accept-rate is not 'better',
    since rejecting namesakes is the right call. Unmeasured ('—') when no gold
    member is in the batch; any gold violation trips the hard gate."""
    mix = _verdict_mix(conn, person_ids)
    metrics = dict(mix)
    v, r, rev = mix.get("verified", 0), mix.get("rejected", 0), mix.get("review", 0)
    mix_caveat = f"mix {v}✓/{rev}?/{r}✗"
    if gold.gold_n == 0:
        return CategoryScore("identity", None, metrics,
                             f"{mix_caveat} — no gold in batch")
    metrics["gold_n"] = gold.gold_n
    metrics["violations"] = list(gold.violations)
    caveat = mix_caveat
    if gold.violations:
        caveat = f"{len(gold.violations)} GOLD VIOLATION(S) — {mix_caveat}"
    return CategoryScore("identity", gold.identity_score, metrics, caveat)


def accuracy_category(gold: GoldReport) -> CategoryScore:
    """Per-field match against the gold answer key (positives only). Unmeasured
    when the batch contains no positive gold member."""
    if gold.accuracy is None:
        reason = ("no positive gold in batch" if gold.gold_n else "no gold in batch")
        return CategoryScore("accuracy", None, {"reason": reason}, "no gold")
    return CategoryScore("accuracy", gold.accuracy,
                         {"positives": gold.positives, "gold_n": gold.gold_n})


def regression_category(
    records: list[PersonRecord], prior: dict[str, dict] | None
) -> CategoryScore:
    """Per-person drops vs the prior scorecard run: completeness fell, or a
    previously-coherent person became incoherent. None on the first ever run."""
    if not prior:
        return CategoryScore("regression", None, {"reason": "no prior run"},
                             "first run")
    now = current_year()
    dropped: list[int] = []
    compared = 0
    for r in records:
        prev = prior.get(str(r.person_id))
        if not prev:
            continue
        compared += 1
        rep = coherence_report(list(r.claims), r.grad_year, now)
        coherent_now = not rep.failures
        if r.completeness < prev.get("completeness", 0) - 1 or \
                (prev.get("coherent") and not coherent_now):
            dropped.append(r.person_id)
    score = _pct(compared - len(dropped), compared) if compared else 100
    caveat = f"{len(dropped)} regressed" if dropped else ""
    return CategoryScore(
        "regression", score,
        {"compared": compared, "dropped": dropped[:20], "drop_count": len(dropped)},
        caveat,
    )


# --------------------------------------------------------------------------- #
# Composite + grade                                                           #
# --------------------------------------------------------------------------- #

def composite_score(categories: dict[str, CategoryScore]) -> int:
    """Weighted mean over measured (non-None) composite categories, with the
    remaining weights renormalized. Regression is informational, not weighted."""
    num = den = 0.0
    for key, weight in WEIGHTS.items():
        cat = categories.get(key)
        if cat is None or cat.score is None:
            continue
        num += weight * cat.score
        den += weight
    return round(num / den) if den else 0


def letter_grade(composite: int, gated: bool) -> str:
    """Letters from the composite; a tripped hard gate caps at REVIEW regardless
    of how high the other scores are (mirrors qa_audit's confidence gate)."""
    if gated:
        return "REVIEW"
    if composite >= 90:
        return "A"
    if composite >= 80:
        return "B"
    if composite >= 70:
        return "C"
    if composite >= 60:
        return "D"
    return "F"


def is_gated(categories: dict[str, CategoryScore]) -> bool:
    """Hard gate: any future-date P0 in coherence, or any gold identity violation
    (must-reject leak / ghost filled). Either caps the batch grade at REVIEW."""
    coh = categories.get("coherence")
    if coh and coh.metrics.get("p0", 0) > 0:
        return True
    ident = categories.get("identity")
    return bool(ident and ident.metrics.get("violations"))


# --------------------------------------------------------------------------- #
# Loading / scoping                                                           #
# --------------------------------------------------------------------------- #

def _load_claims(conn, pid: int) -> tuple[ClaimRow, ...]:
    rows = conn.execute(
        "SELECT claim_type,value,source_url,quote,confidence,extraction_method "
        "FROM claims WHERE person_id=?", (pid,),
    ).fetchall()
    return tuple(
        ClaimRow(r["claim_type"], r["value"], r["source_url"], r["quote"] or "",
                 r["confidence"], r["extraction_method"])
        for r in rows
    )


def _select_people(conn, *, ids, recent, since, do_all) -> list[dict]:
    base = (
        "SELECT p.id, p.full_name, b.updated_at "
        "FROM people p JOIN batch_status b "
        "ON b.person_id=p.id AND b.phase='structuring' AND b.status='done'"
    )
    if ids:
        ph = ",".join("?" * len(ids))
        rows = conn.execute(base + f" WHERE p.id IN ({ph})", ids).fetchall()
    elif recent:
        rows = conn.execute(
            base + " ORDER BY b.updated_at DESC LIMIT ?", (recent,)).fetchall()
    elif since:
        rows = conn.execute(
            base + " WHERE b.updated_at > ? ORDER BY p.id", (since,)).fetchall()
    elif do_all:
        rows = conn.execute(base + " ORDER BY p.id").fetchall()
    else:  # default: since the last scorecard run
        last = last_run_timestamp()
        if last:
            rows = conn.execute(
                base + " WHERE b.updated_at > ? ORDER BY p.id", (last,)).fetchall()
        else:
            rows = conn.execute(base + " ORDER BY p.id").fetchall()
    return [dict(r) for r in rows]


def _build_records(conn, people: list[dict]) -> list[PersonRecord]:
    records = []
    for p in people:
        insight: PersonInsight | None = get_person_insight(conn, p["id"])
        records.append(PersonRecord(
            person_id=p["id"],
            full_name=p["full_name"],
            claims=_load_claims(conn, p["id"]),
            grad_year=insight.grad_year if insight else None,
            completeness=insight.completeness_score if insight else 0,
        ))
    return records


# --------------------------------------------------------------------------- #
# Cost matching                                                               #
# --------------------------------------------------------------------------- #

def _batch_cost_usd(since: str | None) -> float | None:
    """Sum total_usd of cost_log rows newer than `since`. None if the log is
    missing or no rows fall in the window (older batches predate logging)."""
    from cost_log import DEFAULT_LOG_PATH
    if not DEFAULT_LOG_PATH.exists():
        return None
    total = 0.0
    matched = False
    with open(DEFAULT_LOG_PATH) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since and row.get("timestamp", "") <= since:
                continue
            total += float(row.get("total_usd", 0) or 0)
            matched = True
    return round(total, 4) if matched else None


# --------------------------------------------------------------------------- #
# Persistence                                                                 #
# --------------------------------------------------------------------------- #

def _category_to_json(cat: CategoryScore) -> dict:
    return {"score": cat.score, "metrics": cat.metrics, "caveat": cat.caveat}


def load_runs(limit: int | None = None) -> list[dict]:
    """Prior scorecard runs, oldest first. Empty if the log doesn't exist."""
    if not SCORECARD_LOG.exists():
        return []
    runs = []
    with open(SCORECARD_LOG) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                runs.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return runs[-limit:] if limit else runs


def last_run_timestamp() -> str | None:
    runs = load_runs()
    return runs[-1]["timestamp"] if runs else None


def append_run(run: ScorecardRun) -> None:
    payload = {
        "timestamp": run.timestamp,
        "label": run.label,
        "n": run.n,
        "composite": run.composite,
        "grade": run.grade,
        "gated": run.gated,
        "categories": {k: _category_to_json(v) for k, v in run.categories.items()},
        "per_person": run.per_person,
    }
    with open(SCORECARD_LOG, "a") as f:
        f.write(json.dumps(payload) + "\n")


# --------------------------------------------------------------------------- #
# Orchestration                                                               #
# --------------------------------------------------------------------------- #

def score_batch(conn, records: list[PersonRecord], *, label: str,
                cost_since: str | None, prior: dict | None) -> ScorecardRun:
    now = current_year()
    person_ids = [r.person_id for r in records]
    cost_usd = _batch_cost_usd(cost_since)
    verified = sum(1 for r in records if any(
        c.claim_type == "current_employer" for c in r.claims))

    claims_by_id = {r.person_id: list(r.claims) for r in records}
    gold = score_gold(load_gold(), claims_by_id)

    categories = {
        "coverage": coverage_category(records),
        "accuracy": accuracy_category(gold),
        "identity": identity_category(conn, person_ids, gold),
        "richness": richness_category(records),
        "coherence": coherence_category(records),
        "corroboration": corroboration_category(records),
        "cost": cost_category(cost_usd, verified),
        "regression": regression_category(records, prior),
    }
    gated = is_gated(categories)
    composite = composite_score(categories)
    grade = letter_grade(composite, gated)

    per_person = {
        str(r.person_id): {
            "completeness": r.completeness,
            "coherent": not coherence_report(list(r.claims), r.grad_year, now).failures,
        }
        for r in records
    }
    return ScorecardRun(
        timestamp=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        label=label, n=len(records), categories=categories,
        composite=composite, grade=grade, gated=gated, per_person=per_person,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scorecard", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--ids", help="Comma-separated person ids")
    g.add_argument("--recent", type=int, help="The N most recently enriched")
    g.add_argument("--since", help="ISO timestamp; people enriched after it")
    g.add_argument("--all", action="store_true", help="The whole cohort")
    p.add_argument("--label", default="", help="Human label for this batch")
    p.add_argument("--no-save", action="store_true",
                   help="Don't append to scorecard.jsonl")
    p.add_argument("--history", type=int, default=4,
                   help="Prior runs to show as trend columns (default 4)")
    p.add_argument("--llm", action="store_true",
                   help="Add a Sonnet narrative 'top 3 fixes' (one paid call)")
    return p


def _print_llm_narrative(run: ScorecardRun, diag) -> None:
    """Optional paid narrative. Imports the SDK lazily so the default path stays
    LLM-free, and logs the call's cost to cost_log.jsonl."""
    from anthropic import Anthropic

    from config import require_key
    from cost_log import append_entry, build_entry
    from identity import SONNET_MODEL
    from scorecard_diagnose import llm_narrative

    client = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
    run_json = {
        "composite": run.composite, "grade": run.grade, "n": run.n,
        "categories": {k: _category_to_json(v) for k, v in run.categories.items()},
    }
    nar = llm_narrative(run_json, diag, client=client, model=SONNET_MODEL)
    if nar.text:
        print("\nLLM review:")
        print(nar.text)
        append_entry(build_entry(
            label=f"scorecard-narrative:{run.label}", people=run.n,
            haiku_in=0, haiku_out=0, sonnet_in=nar.tokens_in,
            sonnet_out=nar.tokens_out,
        ))
    else:
        print("\n(LLM review unavailable — deterministic diagnosis stands.)")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    ids = [int(x) for x in args.ids.split(",") if x.strip()] if args.ids else None
    prior_runs = load_runs()
    prior = prior_runs[-1]["per_person"] if prior_runs else None
    cost_since = last_run_timestamp()

    with connect(DB_PATH) as conn:
        people = _select_people(conn, ids=ids, recent=args.recent,
                                since=args.since, do_all=args.all)
        if not people:
            print("No people in scope.", file=sys.stderr)
            return 1
        records = _build_records(conn, people)
        label = args.label or (args.since and f"since {args.since}") or \
            (args.recent and f"recent {args.recent}") or \
            (ids and f"ids[{len(ids)}]") or "batch"
        run = score_batch(conn, records, label=str(label),
                          cost_since=cost_since, prior=prior)

    print(render_table(prior_runs, run, history=args.history))

    from scorecard_diagnose import diagnose, render_diagnosis
    diag = diagnose(run)
    print("\n" + render_diagnosis(diag))

    if args.llm:
        _print_llm_narrative(run, diag)

    if not args.no_save:
        append_run(run)
        print(f"\nSaved to {SCORECARD_LOG.relative_to(DATA_DIR.parent)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
