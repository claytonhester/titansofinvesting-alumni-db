"""Append-only LinkedIn refresh for already-enriched people (LinkedIn-first
Phase 1 runner — see docs/linkedin-first-plan.md).

Per person: Firecrawl agent finds the LinkedIn -> the fail-closed roster-anchor
verifier (linkedin_verify) judges it -> ONLY a verified profile's claims are
APPENDED -> the full claim set is re-reconciled (the dated-wins tiebreaker now
upgrades stale undated roles) -> derived insights recomputed. A rejected or
review verdict writes nothing but its identity_candidates audit row.

Ghost rule: the agent fires even for people with ZERO verified web sources.
The old min-verified-sources gate existed because agent output went into claims
unverified; the fail-closed verifier replaces that protection. Searches are
always anchored (name + roster employer + school + city), never bare-name.

Safety, matching research_run.py conventions:
  * DRY-RUN by default — prints scope + credit/$ estimate, no API calls,
    no DB writes. --apply runs for real.
  * --apply backs up the DB first (WAL-checkpointed copy).
  * Hard caps: --max-credits (Firecrawl agent spend, default 4000) and
    --max-usd (Claude spend, default 10.0). Commit per person: resumable.
  * Spend recorded to the cost log.

    python linkedin_refresh.py                          # dry run over the sweep targets
    python linkedin_refresh.py --limit 3 --apply        # pilot the first 3
    python linkedin_refresh.py --ids "770,16" --apply   # explicit people
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic
from firecrawl import Firecrawl
from firecrawl.v2.utils.error_handler import PaymentRequiredError

import config  # noqa: F401 — import triggers load_dotenv() for API keys
from backfill_career_fields import recompute_career_fields
from config import DB_PATH, require_key
from cost_log import append_entry, build_entry, claude_usd, remaining_credits
from db import connect
from enrichment_store import (
    CandidateRow,
    ClaimRow,
    append_claims,
    init_enrichment_schema,
    replace_claims,
    upsert_candidate,
)
from linkedin_firecrawl import DEFAULT_MAX_CREDITS, fetch_linkedin
from linkedin_verify import verify_linkedin_profile
from normalize import digest_claims
from profile_cleanup import clean_profile
from reconcile import reconcile_claims
from structuring import HAIKU_MODEL

DEFAULT_TARGETS_FILE = "data/linkedin_sweep_targets.json"

# Dry-run planning numbers only — real spend is measured, not assumed.
EST_CREDITS_PER_PERSON = DEFAULT_MAX_CREDITS
EST_USD_PER_PERSON = 0.02  # verify + reconcile Haiku calls


def _target_ids(ids_arg: str | None, targets_file: str) -> list[int]:
    """Explicit --ids wins; otherwise every person id in the targets file
    (format: {"people": [...]} or any nested lists of {"id": ...} dicts)."""
    if ids_arg:
        try:
            ids = [int(tok) for tok in ids_arg.split(",") if tok.strip()]
        except ValueError as exc:
            raise SystemExit(f"--ids must be comma-separated integers: {exc}")
        if not ids:
            raise SystemExit("--ids given but no valid IDs parsed")
        return ids

    path = Path(targets_file)
    if not path.exists():
        raise SystemExit(f"targets file not found: {targets_file}")
    try:
        doc = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(f"targets file is not valid JSON: {exc}")

    ids: list[int] = []

    def _walk(node: object) -> None:
        if isinstance(node, dict):
            if isinstance(node.get("id"), int):
                ids.append(node["id"])
            for v in node.values():
                _walk(v)
        elif isinstance(node, list):
            for v in node:
                _walk(v)

    _walk(doc)
    seen: set[int] = set()
    unique = [i for i in ids if not (i in seen or seen.add(i))]
    if not unique:
        raise SystemExit(f"no person ids found in {targets_file}")
    return unique


def _load_people(conn: sqlite3.Connection, ids: list[int]) -> list[sqlite3.Row]:
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT p.id, p.full_name, p.school, p.titan_class, p.initial_company, "
        "p.city, pi.grad_year, "
        "(SELECT c.value FROM claims c WHERE c.person_id = p.id "
        " AND c.claim_type = 'current_employer' LIMIT 1) AS current_employer "
        "FROM people p LEFT JOIN person_insights pi ON pi.person_id = p.id "
        f"WHERE p.id IN ({placeholders}) ORDER BY p.id",
        ids,
    ).fetchall()
    found = {r["id"] for r in rows}
    for missing in (i for i in ids if i not in found):
        print(f"  (id {missing} not in people table — skipped)", file=sys.stderr)
    return rows


def _load_claims(conn: sqlite3.Connection, person_id: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    return [
        ClaimRow(
            claim_type=r["claim_type"],
            value=r["value"],
            source_url=r["source_url"],
            quote=r["quote"],
            confidence=r["confidence"],
            extraction_method=r["extraction_method"],
        )
        for r in rows
    ]


def _profile_url(claims: list[ClaimRow]) -> str:
    """All agent claims carry the profile URL as source_url (map_claims)."""
    for c in claims:
        if c.source_url:
            return c.source_url
    return ""


def refresh_person(
    conn: sqlite3.Connection,
    firecrawl: Firecrawl,
    anthropic: Anthropic,
    person: sqlite3.Row,
) -> tuple[int, int, int, bool]:
    """One person's refresh. Returns (credits_used, haiku_in, haiku_out,
    upgraded). Raises only PaymentRequiredError (out of Firecrawl credits)."""
    pid, name = person["id"], person["full_name"]
    employer_hint = person["current_employer"] or person["initial_company"] or ""

    result = fetch_linkedin(firecrawl, name, employer=employer_hint, city=person["city"] or "")
    if not result.found or not result.claim_rows:
        print(f"  agent: not found ({result.credits_used} credits)")
        return result.credits_used, 0, 0, False

    url = _profile_url(list(result.claim_rows))
    verdict, hin, hout = verify_linkedin_profile(
        anthropic, name,
        profile_url=url,
        school=person["school"] or "",
        grad_year=person["grad_year"],
        roster_employer=person["initial_company"] or "",
        city=person["city"] or "",
        claims=list(result.claim_rows),
    )
    upsert_candidate(conn, pid, CandidateRow(
        source_url=url or f"linkedin-agent:{name}",
        confidence=verdict.confidence,
        decision=verdict.decision,
        reason=verdict.reason or "linkedin-agent profile verdict",
        model=HAIKU_MODEL,
    ))
    if not verdict.verified:
        print(f"  agent: {verdict.decision} — {verdict.reason} "
              f"({result.credits_used} credits)")
        return result.credits_used, hin, hout, False

    # Verified: append, then re-reconcile the FULL set (dated-wins tiebreaker
    # upgrades stale roles), then the standard digest + deterministic cleanup.
    append_claims(conn, pid, list(result.claim_rows))
    full = _load_claims(conn, pid)
    reconciled, rin, rout = reconcile_claims(anthropic, name, full)
    cleaned = clean_profile(digest_claims(reconciled))
    replace_claims(conn, pid, cleaned)
    summary = recompute_career_fields(conn, pid) or "no derived-field changes"
    print(f"  verified ({verdict.confidence:.2f}): +{len(result.claim_rows)} claims, "
          f"{len(full)} -> {len(cleaned)} after reconcile  [{summary}]")
    return result.credits_used, hin + rin, hout + rout, True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="run for real (default: dry run)")
    ap.add_argument("--ids", default=None, help="comma-separated person IDs (overrides --targets-file)")
    ap.add_argument("--targets-file", default=DEFAULT_TARGETS_FILE)
    ap.add_argument("--limit", type=int, default=None, help="first N targets only")
    ap.add_argument("--max-credits", type=int, default=4000,
                    help="hard Firecrawl agent credit ceiling (default 4000)")
    ap.add_argument("--max-usd", type=float, default=10.0,
                    help="hard Claude cost cap in USD (default 10.0)")
    ap.add_argument("--db", default=str(DB_PATH))
    args = ap.parse_args(argv)

    ids = _target_ids(args.ids, args.targets_file)
    if args.limit:
        ids = ids[: args.limit]

    with connect(Path(args.db)) as conn:
        init_enrichment_schema(conn)
        people = _load_people(conn, ids)
    if not people:
        print("No matching people.", file=sys.stderr)
        return 1

    est_credits = EST_CREDITS_PER_PERSON * len(people)
    est_usd = EST_USD_PER_PERSON * len(people)
    print(f"Scope: {len(people)} people | est ~{est_credits} Firecrawl credits "
          f"(cap {args.max_credits}) + ~${est_usd:.2f} Claude (cap ${args.max_usd:.2f})")

    if not args.apply:
        print("\nDRY RUN — no API calls, no DB writes. Re-run with --apply to execute.")
        for p in people:
            anchor = p["current_employer"] or p["initial_company"] or "(no employer anchor)"
            print(f"  [{p['id']:>4}] {p['full_name']:<24} anchor: {anchor}")
        return 0

    firecrawl = Firecrawl(api_key=require_key("FIRECRAWL_API_KEY"))
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{args.db}.bak-pre-linkedin-refresh-{stamp}"
    # Checkpoint the WAL into the main file FIRST, else copy2 captures an
    # inconsistent snapshot (the main .db without the pending -wal frames).
    with connect(Path(args.db)) as _c:
        _c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    shutil.copy2(args.db, backup)
    print(f"Backed up -> {backup}\n")

    credits_before = remaining_credits(firecrawl)
    spent_credits = 0
    haiku_in = haiku_out = 0
    verified = attempted = 0

    with connect(Path(args.db)) as conn:
        for person in people:
            if spent_credits >= args.max_credits:
                print(f"\nCredit cap {args.max_credits} reached ({spent_credits}). Stopping.")
                break
            usd = claude_usd(haiku_in, haiku_out, 0, 0)
            if usd >= args.max_usd:
                print(f"\nCost cap ${args.max_usd:.2f} reached (${usd:.2f}). Stopping.")
                break
            print(f"[{person['id']}] {person['full_name']}")
            attempted += 1
            try:
                credits, hin, hout, upgraded = refresh_person(
                    conn, firecrawl, anthropic, person
                )
                conn.commit()  # persist each person before moving on (resumable)
            except PaymentRequiredError:
                print("\nFIRECRAWL CREDITS EXHAUSTED — stopping. "
                      "Processed people are saved.", file=sys.stderr)
                break
            except Exception as exc:  # noqa: BLE001 — degrade this person, continue
                conn.rollback()
                print(f"  ERROR: {exc}", file=sys.stderr)
                continue
            spent_credits += credits
            haiku_in += hin
            haiku_out += hout
            verified += 1 if upgraded else 0

    credits_after = remaining_credits(firecrawl)
    usd = claude_usd(haiku_in, haiku_out, 0, 0)
    print("\n" + "=" * 56)
    print(f"verified+upgraded: {verified}/{attempted} attempted")
    print(f"firecrawl: ~{spent_credits} credits (meter {credits_before} -> {credits_after}) "
          f"| haiku {haiku_in}+{haiku_out} tok ${usd:.4f}")

    entry = build_entry(
        label="linkedin-refresh", people=attempted,
        haiku_in=haiku_in, haiku_out=haiku_out,
        credits_before=credits_before, credits_after=credits_after,
        estimated_credits=spent_credits,
    )
    append_entry(entry)
    print("Logged to cost log. Next: ./finalize_pass.sh, then commit web/data/titans.db.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
