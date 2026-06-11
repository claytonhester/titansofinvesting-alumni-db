"""Propose the next people to hand-verify into the gold set.

Growing the answer key shouldn't mean digging through the DB. This finds the
highest-confidence profiles NOT already in gold — a current role, a LinkedIn to
check against, internally coherent — ranks them by completeness, and prints each
with a paste-ready gold record pre-filled from the pipeline's current values.

The workflow: run `scorecard.py --gold-candidates`, open each LinkedIn, and tell
the assistant which ids are correct. Confirmed ones get promoted to
source:"human-verified" in eval/gold.json — turning the Accuracy row from a
self-referential regression guard into a real, adversarial correctness test.

Pure read-only: loads claims, filters, ranks. No writes, no LLM, no network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from career_analysis import career_entries
from coherence import coherence_report
from enrichment_store import ClaimRow
from person_insights_store import get_person_insight
from reconcile import RECONCILE_METHOD_SUFFIX, _source_family


@dataclass(frozen=True)
class GoldCandidate:
    person_id: int
    full_name: str
    current_title: str
    current_employer: str
    education: tuple[str, ...]
    linkedin_url: str
    completeness: int
    corroborated: bool  # current employer confirmed by 2+ sources


def _load_claims(conn, pid: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type,value,source_url,quote,confidence,extraction_method "
        "FROM claims WHERE person_id=?", (pid,),
    ).fetchall()
    return [ClaimRow(r["claim_type"], r["value"], r["source_url"], r["quote"] or "",
                     r["confidence"], r["extraction_method"]) for r in rows]


def _first(claims: list[ClaimRow], claim_type: str) -> ClaimRow | None:
    return next((c for c in claims if c.claim_type == claim_type), None)


def _linkedin_url(claims: list[ClaimRow]) -> str:
    for c in claims:
        url = (c.source_url or "").lower()
        if c.claim_type == "public_links" and "linkedin.com/in/" in url:
            return c.source_url
    return ""


def _is_corroborated(method: str) -> bool:
    if not (method or "").endswith(RECONCILE_METHOD_SUFFIX):
        return False
    prefix = method[: -len(RECONCILE_METHOD_SUFFIX)]
    return len({_source_family(p) for p in prefix.split("+") if p}) >= 2


def find_candidates(conn, exclude_ids: set[int], *, limit: int = 10,
                    require_corroborated: bool = False) -> list[GoldCandidate]:
    """Coherent profiles with a current role + LinkedIn, not already in gold,
    ranked by completeness (then corroboration). A human can verify each against
    its LinkedIn in seconds. require_corroborated narrows to multi-source roles."""
    rows = conn.execute("SELECT id, full_name FROM people ORDER BY id").fetchall()
    out: list[GoldCandidate] = []
    for r in rows:
        pid = r["id"]
        if pid in exclude_ids:
            continue
        claims = _load_claims(conn, pid)
        emp = _first(claims, "current_employer")
        ttl = _first(claims, "current_title")
        li = _linkedin_url(claims)
        if not (emp and ttl and li):
            continue
        if coherence_report(claims, None, _now_year()).failures:
            continue
        corrob = _is_corroborated(emp.extraction_method)
        if require_corroborated and not corrob:
            continue
        ins = get_person_insight(conn, pid)
        edu = tuple(c.value for c in claims if c.claim_type == "education")[:2]
        out.append(GoldCandidate(
            person_id=pid, full_name=r["full_name"],
            current_title=ttl.value, current_employer=emp.value,
            education=edu, linkedin_url=li,
            completeness=ins.completeness_score if ins else 0,
            corroborated=corrob,
        ))
    out.sort(key=lambda c: (c.completeness, c.corroborated), reverse=True)
    return out[:limit]


def _now_year() -> int:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).year


def _short_school(edu_value: str) -> str:
    """Trim a verbose education line to the school for the paste-ready record."""
    v = edu_value.split(" From ", 1)[-1] if " From " in edu_value else edu_value
    return v.split("(")[0].strip()[:60]


def _record_snippet(c: GoldCandidate) -> str:
    rec = {
        "person_id": c.person_id,
        "full_name": c.full_name,
        "verified_on": "",
        "source": "human-verified",
        "expect": {
            "current_employer": c.current_employer,
            "current_title": c.current_title,
            "education": [_short_school(e) for e in c.education],
            "linkedin_url": c.linkedin_url,
        },
        "must_reject_urls": [],
        "must_stay_empty": False,
    }
    return json.dumps(rec, indent=2)


def render_candidates(cands: list[GoldCandidate]) -> str:
    if not cands:
        return ("No new candidates — every coherent profile with a current role "
                "and LinkedIn is already in the gold set.")
    lines = [
        f"{len(cands)} candidate(s) to hand-verify "
        "(open each LinkedIn, then tell me which ids are correct):",
        "",
    ]
    for c in cands:
        flag = " [corroborated]" if c.corroborated else ""
        lines.append(f"  #{c.person_id}  {c.full_name}  (completeness {c.completeness}{flag})")
        lines.append(f"      {c.current_title} @ {c.current_employer}")
        if c.education:
            lines.append(f"      edu: {', '.join(_short_school(e) for e in c.education)}")
        lines.append(f"      verify: {c.linkedin_url}")
        lines.append("")
    lines.append("Reply e.g. \"add 766, 764\" and I'll promote those to "
                 "source:human-verified in eval/gold.json.")
    lines.append("")
    lines.append("Paste-ready records (pre-filled from current data — correct any "
                 "field that's wrong before adding):")
    for c in cands:
        lines.append(_record_snippet(c) + ",")
    return "\n".join(lines)
