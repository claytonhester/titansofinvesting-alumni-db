"""Pre-flight go/no-go check + post-run "couldn't enrich" report for an
enrichment run. Read-only: spends nothing, changes nothing.

    python preflight.py            # pre-flight checks for a --rerun-enriched base sweep
    python preflight.py --report   # who couldn't be enriched after a run

Pre-flight verifies the things that silently ruin a run: missing OR REJECTED
keys (each configured key makes one cheap authenticated call — a key that is
set but wrong used to pass this check and then silently empty every result),
no DB backup, an empty target set, and shows the Firecrawl balance + a rough
cost. Exit code 0 = GO, 1 = NO-GO (something a human must fix first).
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx

from config import DB_PATH
from cost_log import PDL_USD_PER_MATCH, remaining_credits
from db import connect
from pdl_enrich import PDL_ENRICH_URL
from perplexity_enrich import PERPLEXITY_SEARCH_URL
from structuring import HAIKU_MODEL

ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"
FIRECRAWL_CREDIT_USAGE_URL = "https://api.firecrawl.dev/v2/team/credit-usage"

AUTH_OK = "OK"
AUTH_FAIL = "AUTH-FAIL"
AUTH_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class AuthCheck:
    status: str   # AUTH_OK | AUTH_FAIL | AUTH_UNKNOWN
    detail: str


def _auth_probe(name: str, key: str, http: httpx.Client) -> httpx.Response:
    """The cheapest request that still exercises the key. Every probe is
    read-only; two of them are deliberately INVALID requests, because the API
    rejects a bad key (401) before it validates the body (400), so a 400 proves
    the key at zero cost."""
    if name == "ANTHROPIC_API_KEY":
        # One output token on Haiku — a fraction of a cent.
        return http.post(
            ANTHROPIC_MESSAGES_URL,
            headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
            json={"model": HAIKU_MODEL, "max_tokens": 1,
                  "messages": [{"role": "user", "content": "ping"}]},
        )
    if name == "FIRECRAWL_API_KEY":
        # The same credit-usage read the run itself makes; free.
        return http.get(FIRECRAWL_CREDIT_USAGE_URL,
                        headers={"Authorization": f"Bearer {key}"})
    if name == "PDL_API_KEY":
        # No params -> 400 "insufficient parameters" once the key is accepted;
        # nothing is matched, so nothing is billed.
        return http.get(PDL_ENRICH_URL, headers={"X-Api-Key": key, "Accept": "application/json"})
    if name == "PERPLEXITY_API_KEY":
        # Empty body -> 400 validation error on a good key; no search runs.
        return http.post(PERPLEXITY_SEARCH_URL,
                         headers={"Authorization": f"Bearer {key}"}, json={})
    raise ValueError(f"no auth probe defined for {name}")


def check_key_auth(name: str, key: str, http: httpx.Client) -> AuthCheck:
    """One cheap authenticated call for `name`. 401/403 -> AUTH_FAIL; a 5xx or
    a network error -> AUTH_UNKNOWN (the key could not be judged); anything
    else (2xx, or the expected 400 for the deliberately-invalid probes) ->
    AUTH_OK. Never raises."""
    try:
        resp = _auth_probe(name, key, http)
    except Exception as exc:  # noqa: BLE001 — report, don't crash preflight
        return AuthCheck(AUTH_UNKNOWN, f"unreachable: {exc}")
    if resp.status_code in (401, 403):
        return AuthCheck(AUTH_FAIL, f"HTTP {resp.status_code} — key rejected")
    if resp.status_code >= 500:
        return AuthCheck(AUTH_UNKNOWN, f"HTTP {resp.status_code} — service error")
    return AuthCheck(AUTH_OK, f"HTTP {resp.status_code}")

# Rough per-person cost of a base sweep (PDL match + Perplexity search/mentions +
# Anthropic identity/structure/bio). Firecrawl is 0 at --max-credits 0. Empirical
# from the canary: ~$0.39/person.
BASE_SWEEP_USD_PER_PERSON = 0.40


def _keys() -> list[tuple[str, bool, bool]]:
    """(name, present, required) — required keys block the run; soft keys degrade it."""
    return [
        ("ANTHROPIC_API_KEY", bool(os.getenv("ANTHROPIC_API_KEY")), True),
        ("FIRECRAWL_API_KEY", bool(os.getenv("FIRECRAWL_API_KEY")), True),  # client ctor
        ("PDL_API_KEY", bool(os.getenv("PDL_API_KEY")), False),
        ("PERPLEXITY_API_KEY", bool(os.getenv("PERPLEXITY_API_KEY")), False),
    ]


def _latest_backup() -> Path | None:
    backups = sorted(Path(DB_PATH).parent.glob("titans.backup.*.db"))
    return backups[-1] if backups else None


def _rerun_target_count(conn) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM people p JOIN person_insights pi ON pi.person_id = p.id"
    ).fetchone()[0]


def preflight(check_auth: bool = True) -> int:
    print("\n=== PRE-FLIGHT: --rerun-enriched base sweep ===\n")
    ok = True

    print("API keys:" + ("" if check_auth else " (auth check skipped)"))
    with httpx.Client(timeout=20.0) as http:
        for name, present, required in _keys():
            tag = "REQUIRED" if required else "soft"
            if not present:
                mark = "✗ MISSING" if required else "— (degrades)"
                print(f"  {mark:<14} {name} [{tag}]")
                if required:
                    ok = False
                continue
            if not check_auth:
                print(f"  {'✓ set':<14} {name} [{tag}]")
                continue
            check = check_key_auth(name, os.environ[name], http)
            mark = {AUTH_OK: "✓ OK", AUTH_FAIL: "✗ AUTH-FAIL"}.get(check.status, "? UNKNOWN")
            print(f"  {mark:<14} {name} [{tag}] {check.detail}")
            # A key that is SET but WRONG is a NO-GO even for a soft key: the
            # run would not degrade, it would silently produce nothing from
            # that source (and phase2 now aborts on the first 401 anyway).
            if check.status == AUTH_FAIL:
                ok = False

    backup = _latest_backup()
    print("\nDB backup:")
    if backup:
        print(f"  ✓ latest: {backup.name}")
    else:
        print("  ✗ NO backup found — make one before a rerun "
              "(cp data/titans.db data/titans.backup.<date>-<label>.db)")
        ok = False

    # Firecrawl balance is informational for a base sweep (--max-credits 0 spends
    # none), but a NO-GO for a deep pass.
    print("\nFirecrawl balance:")
    try:
        from firecrawl import Firecrawl
        fc = Firecrawl(api_key=os.getenv("FIRECRAWL_API_KEY"))
        print(f"  {remaining_credits(fc)} credits (base sweep spends 0; matters for the deep pass)")
    except Exception as exc:  # noqa: BLE001
        print(f"  — could not read ({exc})")

    with connect(Path(DB_PATH)) as conn:
        n = _rerun_target_count(conn)
    print("\nTargets:")
    print(f"  {n} already-enriched people (--rerun-enriched)")
    print(f"  est. cost ≈ ${n * BASE_SWEEP_USD_PER_PERSON:,.2f} "
          f"(~${BASE_SWEEP_USD_PER_PERSON:.2f}/person, Firecrawl $0)")
    if n == 0:
        print("  ✗ nothing to rerun")
        ok = False

    print(f"\n=== {'GO' if ok else 'NO-GO — fix the ✗ items above'} ===\n")
    return 0 if ok else 1


def report() -> int:
    """Who couldn't be enriched: errored, still-thin (flagged), or zero-claim."""
    with connect(Path(DB_PATH)) as conn:
        errored = conn.execute(
            "SELECT b.person_id, p.full_name, b.last_error FROM batch_status b "
            "JOIN people p ON p.id = b.person_id "
            "WHERE b.phase='structuring' AND b.status='error' ORDER BY b.person_id"
        ).fetchall()
        thin = conn.execute(
            "SELECT pi.person_id, p.full_name, pi.completeness_score, pi.deep_search_reason "
            "FROM person_insights pi JOIN people p ON p.id = pi.person_id "
            "WHERE pi.needs_deep_search=1 ORDER BY pi.completeness_score, pi.person_id"
        ).fetchall()
        zero = conn.execute(
            "SELECT p.id, p.full_name FROM people p "
            "JOIN person_insights pi ON pi.person_id = p.id "
            "WHERE NOT EXISTS (SELECT 1 FROM claims c WHERE c.person_id = p.id) "
            "ORDER BY p.id"
        ).fetchall()

    print(f"\n=== COULDN'T FULLY ENRICH ===\n")
    print(f"ERRORED ({len(errored)}) — rolled back, safe to re-run:")
    for r in errored:
        print(f"  [{r['person_id']:>4}] {r['full_name']:<26} {(r['last_error'] or '')[:60]}")
    print(f"\nZERO CLAIMS ({len(zero)}) — no data found (likely genuine ghosts):")
    for r in zero:
        print(f"  [{r['id']:>4}] {r['full_name']}")
    print(f"\nTHIN / FLAGGED FOR DEEP ({len(thin)}) — enriched but incomplete:")
    for r in thin:
        print(f"  [{r['person_id']:>4}] {r['completeness_score']:>3}  "
              f"{r['full_name']:<26} {r['deep_search_reason']}")
    print()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true",
                    help="post-run report of who couldn't be enriched")
    ap.add_argument("--no-auth-check", dest="auth_check", action="store_false",
                    help="only check that keys are SET (skip the per-key auth call)")
    args = ap.parse_args(argv)
    return report() if args.report else preflight(check_auth=args.auth_check)


if __name__ == "__main__":
    sys.exit(main())
