"""Backfill person↔firm links for the EXISTING cohort with no new spend.

People enriched before we captured PDL experience domains have no person_company
rows. Rather than re-pay PDL, we match each person's career_history claims (and
their current employer) by NAME against firms already in the `companies` table,
creating past/current links. This surfaces institutional memory we already paid
for — e.g. Kimberly is at TRS now while Ross, Travis, and Karn passed through it.

Going forward, phase2 writes exact person_company links from PDL experience[]; this
is purely a one-time catch-up for already-enriched people. Run:

    python backfill_person_company.py
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from pathlib import Path

from company_enrich import _emp_tokens
from config import DB_PATH
from db import connect
from person_company_store import (
    PersonCompany,
    init_person_company_schema,
    replace_person_companies,
)

_YEARS_RE = re.compile(r"\((\d{4})\s*[-–]\s*(\d{4}|present)\)\s*$", re.IGNORECASE)
# A trailing SINGLE year — 'Endowment Intern at UTIMCO (2020)'. Left in, the '(2020)'
# rides into the firm name and defeats the company match ('UTIMCO (2020)' != 'UTIMCO').
_SINGLE_YEAR_RE = re.compile(r"\((\d{4})\)\s*$")


def _parse_career(value: str) -> tuple[str, str, int | None, int | None, bool]:
    """(title, company, start_year, end_year, is_present) from a career_history
    value like 'Analyst at Citi (2015-2017)', 'Intern at UTIMCO (2020)', or
    'VP at Sage'. Tolerant."""
    v = value.strip()
    start = end = None
    is_present = False
    m = _YEARS_RE.search(v)
    if m:
        start = int(m.group(1))
        if m.group(2).lower() == "present":
            is_present = True
        else:
            end = int(m.group(2))
        v = v[: m.start()].strip()
    else:
        m1 = _SINGLE_YEAR_RE.search(v)
        if m1:
            start = int(m1.group(1))
            v = v[: m1.start()].strip()
    # Split title / company on the LAST ' at '.
    idx = v.lower().rfind(" at ")
    if idx != -1:
        title, company = v[:idx].strip(), v[idx + 4:].strip()
    else:
        title, company = "", v
    return title, company, start, end, is_present


def _subset_match(career_name: str, companies: list[tuple[str, str]]) -> str:
    """Match a career firm name to an enriched company domain. Requires one firm's
    significant tokens to be a SUBSET of the other's (after dropping geo/generic
    tokens) — so 'Sage Advisory' matches 'Sage Advisory' but not 'Sage
    Therapeutics' — with an acronym fallback (TPH, BPC). '' when no confident match.

    Two precision guards prevent generic-token false links:
      * A subset resting on a SINGLE shared token only counts when the token sets
        are EQUAL — not a proper subset. 'Lincoln International' -> {'lincoln'} is a
        proper subset of 'Lincoln Financial Group' -> {'lincoln','financial'}, but
        the extra distinctive token means they are different firms, so no match. The
        genuine 'Lincoln International LLC' -> {'lincoln'} still matches by equality.
      * The acronym fallback requires >= 3 letters: 2-letter acronyms collide too
        readily ('Shift Admin' and 'Sage Advisory' both -> 'sa')."""
    ct = set(_emp_tokens(career_name))
    if not ct:
        return ""
    cac = "".join(t[0] for t in _emp_tokens(career_name))
    for domain, name in companies:
        nt = set(_emp_tokens(name))
        if not nt:
            continue
        if ct <= nt or nt <= ct:
            smaller = min(len(ct), len(nt))
            if ct == nt or smaller >= 2:
                return domain
        if len(cac) >= 3 and cac == "".join(t[0] for t in _emp_tokens(name)):
            return domain
    return ""


def backfill(db_path: str = str(DB_PATH)) -> int:
    with connect(Path(db_path)) as conn:
        init_person_company_schema(conn)
        companies = [
            (r["domain"], r["name"])
            for r in conn.execute("SELECT domain, name FROM companies WHERE matched = 1")
        ]
        if not companies:
            print("No enriched companies yet — run company_enrich first.", file=sys.stderr)
            return 1
        # The CANONICAL firm name per domain. We display this instead of the raw
        # career-history string, which carries noise the match tolerates but a label
        # should not: a leaked title ('Associate, Teacher Retirement System of Texas'),
        # a year suffix ('UTIMCO (2020)'), or a spelling variant ('TPH&Co.' vs
        # 'TPH & Co.') that would otherwise split one firm across two names.
        name_by_domain = {domain: name for domain, name in companies}

        people = conn.execute(
            "SELECT DISTINCT person_id FROM claims WHERE claim_type = 'career_history'"
        ).fetchall()

        total_links = people_linked = 0
        for (pid,) in people:
            # Skip people who already have PDL-sourced links (don't clobber exact data).
            has_pdl = conn.execute(
                "SELECT 1 FROM person_company WHERE person_id = ? AND source = 'pdl' LIMIT 1",
                (pid,),
            ).fetchone()
            if has_pdl:
                continue

            current_emp = (conn.execute(
                "SELECT value FROM claims WHERE person_id = ? AND claim_type='current_employer' LIMIT 1",
                (pid,),
            ).fetchone() or [""])[0] or ""
            current_domain = _subset_match(current_emp, companies) if current_emp else ""

            careers = conn.execute(
                "SELECT value FROM claims WHERE person_id = ? AND claim_type='career_history'",
                (pid,),
            ).fetchall()

            by_domain: dict[str, PersonCompany] = {}
            for (val,) in careers:
                title, company, start, end, is_present = _parse_career(val)
                domain = _subset_match(company, companies)
                if not domain:
                    continue
                is_current = is_present or domain == current_domain
                link = PersonCompany(
                    person_id=pid, domain=domain,
                    company_name=name_by_domain.get(domain) or company, title=title,
                    start_year=start, end_year=None if is_current else end,
                    is_current=is_current, source="career-match",
                )
                # Keep the most informative role per (person, firm): prefer current,
                # then the one with a start year.
                prev = by_domain.get(domain)
                if prev is None or (is_current and not prev.is_current) or (
                    start and not prev.start_year
                ):
                    by_domain[domain] = link

            # Also ensure the current employer is linked even if not in career_history.
            if current_domain and current_domain not in by_domain:
                by_domain[current_domain] = PersonCompany(
                    person_id=pid, domain=current_domain,
                    company_name=name_by_domain.get(current_domain) or current_emp,
                    title="", start_year=None, end_year=None, is_current=True,
                    source="career-match",
                )

            # Replace unconditionally (an empty set CLEARS stale career-match links
            # from a prior run — e.g. a firm that no longer matches under stricter
            # rules). Safe: people with PDL links were skipped above, so we only ever
            # rewrite a person's own career-match rows.
            replace_person_companies(conn, pid, list(by_domain.values()))
            if by_domain:
                total_links += len(by_domain)
                people_linked += 1
        conn.commit()
        print(f"Linked {total_links} firm relationships across {people_linked} people "
              f"(name-matched to {len(companies)} enriched firms).")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="backfill-person-company", description=__doc__)
    ap.add_argument("--db", default=str(DB_PATH))
    return backfill(ap.parse_args(argv).db)


if __name__ == "__main__":
    sys.exit(main())
