"""Quality eval loop for the enrichment stack — the "machine that learns".

After each test batch this does two things, both logged to
``data/quality_log.jsonl`` so improvement is measured, not vibed:

1. Deterministic METRICS per profile — claims by source family (firecrawl / pdl /
   linkedin / perplexity / merged), coverage flags (current role, bio, LinkedIn),
   counts. Free, no LLM.
2. An LLM AUDITOR — a Sonnet judge scores each profile against a rubric and emits
   P0/P1/P2 issues (P0 = wrong-person data or missing current role; P1 = notable
   gap/dup/inconsistency; P2 = cosmetic). This scales review past eyeballs and
   accumulates "what's failing" as data to target the next fix.

Confidence gate: when consecutive batches audit with zero P0 and <=1 P1, the
stack is trustworthy enough to scale to a big group.

    python qa_audit.py --recent 3        # audit the 3 most recently enriched
    python qa_audit.py --name "Bryan Farney"
    python qa_audit.py --all
    python qa_audit.py --recent 3 --no-llm   # metrics only, skip the LLM auditor

Never raises on the LLM side: an audit failure is recorded as such, so a logging
pass can't crash a test run.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone

from anthropic import Anthropic

from config import DATA_DIR, DB_PATH, require_key
from db import connect
from enrichment_store import ClaimRow
from identity import SONNET_MODEL
from reconcile import RECONCILE_METHOD_SUFFIX, _source_family

# Absolute, so `qa_audit` logs to the right place regardless of cwd (matches cost_log).
QUALITY_LOG = DATA_DIR / "quality_log.jsonl"

_RUBRIC = """You are a strict QA auditor for an alumni intelligence directory. \
Each profile is assembled from multiple sources (Firecrawl web scrape, a LinkedIn \
agent, PeopleDataLabs, and Perplexity mentions) about ONE finance/investing \
professional — an alumnus of a Texas university program.

CRITICAL — how to read the anchors: the "employer on record" and "city" are the \
person's details AT THE TIME THEY WERE IN THE PROGRAM, often years ago. They are \
a STALE starting point, NOT the expected current state. An alumnus who has since \
moved to a different employer, city, or seniority is the NORMAL, CORRECT case — \
that is career progression, NOT a wrong-person error. The anchor's only hard job \
is the NAME and a PLAUSIBLE career arc (a finance/investing professional whose \
history could credibly start from that program). Do NOT penalize, and do NOT call \
a namesake splice, simply because the current employer/city differs from the \
anchor. Only treat differing facts as a namesake problem when they are MUTUALLY \
INCONSISTENT WITH EACH OTHER (e.g. two simultaneous full-time current roles in \
unrelated fields/geographies that no single career path explains).

Judge ONLY what is shown; do not invent. Score each dimension 1-5 (5 = excellent, \
1 = broken):
- current_role: is there exactly one sensible current title + employer? (It need \
NOT match the anchor employer — a newer employer scores full marks.)
- career: is the work history complete, dated, and free of duplicate roles? \
(A role spanning a single year, e.g. "(2018-2018)", that duplicates a longer \
"(2018-present)" role at the same employer is a zero-duration artifact — flag it.)
- education: clean, grouped, no junk (e.g. an airline listed as a school)?
- bio: does the bio describe the person accurately and lead with their CURRENT \
role (not a former one), without adding anything not present in the facts?
- identity: do the assembled facts cohere AS A SINGLE PERSON — i.e. are they \
mutually consistent with one plausible career arc? Judge internal consistency, \
NOT agreement with the (stale) anchor. A stray role/degree from a different \
field/geography that cannot fit one career = a namesake splice.
- formatting: professional casing, clean titles, no scrape artifacts?

Then list concrete issues. Severity:
- P0: facts that cannot belong to one person (genuine namesake splice — mutually \
inconsistent, not merely anchor-divergent), corrupted core facts, or NO current \
role present at all. A current role that simply differs from the anchor is NOT P0.
- P1: a missing/duplicated role, a zero-duration role artifact, a bio that leads \
with a stale role, an internal inconsistency, a thin/uncertain entry.
- P2: cosmetic (casing, a slightly messy title).

Return ONLY JSON:
{"scores": {"current_role": int, "career": int, "education": int, "bio": int, \
"identity": int, "formatting": int}, "issues": [{"severity": "P0|P1|P2", \
"dimension": "<name>", "value": "<offending text or ''>", "detail": "<short>"}], \
"summary": "<one line>"}"""


@dataclass(frozen=True)
class AuditResult:
    person: str
    metrics: dict
    scores: dict
    issues: list
    summary: str
    error: str | None = None

    @property
    def counts(self) -> dict:
        c = Counter(i.get("severity", "P2") for i in self.issues)
        return {"P0": c.get("P0", 0), "P1": c.get("P1", 0), "P2": c.get("P2", 0)}


def compute_metrics(claims: list[ClaimRow]) -> dict:
    """Deterministic per-profile metrics — no LLM. Source families are derived
    from extraction_method, including the multi-source '+reconciled' tags so a
    merged fact counts toward every source that confirmed it."""
    by_source: Counter = Counter()
    for c in claims:
        method = c.extraction_method or ""
        if not method:
            continue
        # Only a '+reconciled' tag encodes MULTIPLE source families (e.g.
        # 'firecrawl+pdl+reconciled'). A plain method name can itself contain '+'
        # ('perplexity+haiku-verify') and must be treated as ONE source.
        if method.endswith(RECONCILE_METHOD_SUFFIX):
            prefix = method[: -len(RECONCILE_METHOD_SUFFIX)]
            fams = {_source_family(p) for p in prefix.split("+") if p}
        else:
            fams = {_source_family(method)}
        for fam in fams:
            by_source[fam] += 1

    career = [c for c in claims if c.claim_type == "career_history"]
    edu = [c for c in claims if c.claim_type == "education"]
    mentions = [c for c in claims if c.claim_type == "public_links"]
    return {
        "total_claims": len(claims),
        "by_source": dict(by_source),
        "career_count": len(career),
        "education_count": len(edu),
        "mention_count": len(mentions),
        "has_current_employer": any(c.claim_type == "current_employer" for c in claims),
        "has_current_title": any(c.claim_type == "current_title" for c in claims),
        "has_bio": any(c.claim_type == "short_bio" for c in claims),
        "has_linkedin": any("linkedin" in (c.source_url or "").lower() for c in mentions),
        "pdl_present": by_source.get("pdl", 0) > 0,
        "firecrawl_present": any(
            by_source.get(f, 0) > 0 for f in ("firecrawl", "firecrawl_news", "firecrawl_linkedin")
        ),
    }


def _profile_text(claims: list[ClaimRow]) -> str:
    by_type: dict[str, list[str]] = {}
    for c in claims:
        by_type.setdefault(c.claim_type, []).append(c.value)
    order = ["current_title", "current_employer", "location", "short_bio",
             "career_history", "education", "public_links"]
    lines = []
    for t in order:
        for v in by_type.get(t, []):
            lines.append(f"  [{t}] {v}")
    return "\n".join(lines)


def _parse_audit(text: str) -> dict | None:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        s, e = cleaned.find("{"), cleaned.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(cleaned[s : e + 1])
            except json.JSONDecodeError:
                return None
    return None


def audit_person(
    client: Anthropic | None,
    name: str,
    employer: str,
    city: str,
    claims: list[ClaimRow],
    *,
    model: str = SONNET_MODEL,
    use_llm: bool = True,
) -> AuditResult:
    metrics = compute_metrics(claims)
    if not use_llm or client is None:
        return AuditResult(name, metrics, {}, [], "(metrics only)", error=None)

    user = (
        f"Anchors:\n  Name: {name}\n  Employer on record: {employer or '(unknown)'}\n"
        f"  City: {city or '(unknown)'}\n\nAssembled facts:\n{_profile_text(claims)}\n\nAudit now."
    )
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1500,
            system=[{"type": "text", "text": _RUBRIC, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text")
    except Exception as exc:  # pragma: no cover - network
        return AuditResult(name, metrics, {}, [], "audit call failed", error=str(exc))

    parsed = _parse_audit(text)
    if not parsed:
        return AuditResult(name, metrics, {}, [], "unparseable audit", error="parse")
    issues = parsed.get("issues") or []
    return AuditResult(
        person=name,
        metrics=metrics,
        scores=parsed.get("scores") or {},
        issues=issues if isinstance(issues, list) else [],
        summary=str(parsed.get("summary") or ""),
    )


def _load_people(conn, names, recent, do_all):
    base = """
        SELECT p.id, p.full_name, p.initial_company AS company, p.city, b.updated_at
        FROM people p
        JOIN batch_status b ON b.person_id=p.id AND b.phase='structuring' AND b.status='done'
    """
    if names:
        ph = ",".join("?" * len(names))
        rows = conn.execute(base + f" WHERE p.full_name IN ({ph})", names).fetchall()
    elif recent:
        rows = conn.execute(base + " ORDER BY b.updated_at DESC LIMIT ?", (recent,)).fetchall()
    else:
        rows = conn.execute(base + " ORDER BY p.id").fetchall()
    return [dict(r) for r in rows]


def _load_claims(conn, pid) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type,value,source_url,quote,confidence,extraction_method "
        "FROM claims WHERE person_id=?", (pid,)
    ).fetchall()
    return [ClaimRow(r["claim_type"], r["value"], r["source_url"], r["quote"] or "",
                     r["confidence"], r["extraction_method"]) for r in rows]


def _append_log(result: AuditResult, stamp: str) -> None:
    entry = {
        "timestamp": stamp,
        "person": result.person,
        "metrics": result.metrics,
        "scores": result.scores,
        "issue_counts": result.counts,
        "issues": result.issues,
        "summary": result.summary,
        "error": result.error,
    }
    with open(QUALITY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def run(names, recent, do_all, use_llm, model) -> int:
    client = Anthropic(api_key=require_key("ANTHROPIC_API_KEY")) if use_llm else None
    stamp = datetime.now(timezone.utc).isoformat()
    with connect(DB_PATH) as conn:
        people = _load_people(conn, names, recent, do_all)
        if not people:
            print("No enriched people to audit.", file=sys.stderr)
            return 1
        print(f"Auditing {len(people)} profiles "
              f"({'LLM + metrics' if use_llm else 'metrics only'})\n")
        tot = Counter()
        for p in people:
            claims = _load_claims(conn, p["id"])
            res = audit_person(client, p["full_name"], p["company"], p["city"],
                               claims, model=model, use_llm=use_llm)
            _append_log(res, stamp)
            m = res.metrics
            src = ", ".join(f"{k}:{v}" for k, v in sorted(m["by_source"].items()))
            print(f"=== {p['full_name']} ===")
            print(f"  metrics: {m['career_count']} roles, {m['education_count']} edu, "
                  f"{m['mention_count']} mentions | sources: {src} | "
                  f"LinkedIn:{'Y' if m['has_linkedin'] else 'N'} bio:{'Y' if m['has_bio'] else 'N'}")
            if use_llm and not res.error:
                c = res.counts
                tot.update(c)
                sc = " ".join(f"{k}:{v}" for k, v in res.scores.items())
                print(f"  audit: {sc}  [P0:{c['P0']} P1:{c['P1']} P2:{c['P2']}]")
                for i in res.issues:
                    if i.get("severity") in ("P0", "P1"):
                        print(f"    {i.get('severity')} {i.get('dimension')}: "
                              f"{i.get('detail')}  ({i.get('value','')[:60]})")
                if res.summary:
                    print(f"  > {res.summary}")
            elif res.error:
                print(f"  audit error: {res.error}")
            print()

        if use_llm:
            print("─" * 56)
            gate = "PASS" if tot["P0"] == 0 and tot["P1"] <= 1 else "REVIEW"
            print(f"BATCH TOTAL  P0:{tot['P0']} P1:{tot['P1']} P2:{tot['P2']}  -> {gate}")
            print("Confidence gate: scale up after consecutive batches with P0=0 and P1<=1.")
        print(f"\nLogged to {QUALITY_LOG}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="qa-audit", description=__doc__)
    g = p.add_mutually_exclusive_group()
    g.add_argument("--recent", type=int, help="Audit the N most recently enriched")
    g.add_argument("--name", action="append", dest="names", help="Audit a specific person (repeatable)")
    g.add_argument("--all", action="store_true", help="Audit everyone enriched")
    p.add_argument("--no-llm", action="store_true", help="Metrics only, skip the LLM auditor")
    p.add_argument("--model", default=SONNET_MODEL, help="Auditor model")
    return p


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    return run(a.names, a.recent, a.all, use_llm=not a.no_llm, model=a.model)


if __name__ == "__main__":
    import config  # noqa: F401  (loads .env)
    sys.exit(main())
