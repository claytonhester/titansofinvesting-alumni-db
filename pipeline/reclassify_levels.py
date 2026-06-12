"""Reclassify the cohort onto the cross-industry ladder (seniority_v2) and
recompute the two product thresholds — all from data we ALREADY hold. No
enrichment calls; the only (tiny) spend is Haiku classifying roles not yet in
the role_level_cache.

Pipeline:
  1. Gather every (title, employer, start_year) role per person, from
     career_history claims AND person_company, plus the person's sector hint.
  2. Classify each DISTINCT (title, employer) — cache hits are free; only the
     misses go to Haiku (sector-aware, temp 0). Results are cached with
     provenance so the next run is near-instant.
  3. Per person: peak rung, reached_manager / reached_senior_leadership, and
     years_to_manager / years_to_senior_leadership (first qualifying role start
     minus grad year, clamped at 0).
  4. Write the derived tags to person_insights (the public title is untouched)
     and print an old-vs-new comparison.

Run:   python reclassify_levels.py [--db data/titans.db] [--dry-run] [--no-llm]
       [--version N]   # bump to invalidate the cache and force a clean re-run
"""
from __future__ import annotations

import argparse
import sqlite3
import statistics
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic

from career_analysis import career_entries
from config import require_key
from enrichment_store import ClaimRow
from person_insights_store import init_person_insights_schema
from role_level_store import (
    init_role_level_schema,
    load_cached,
    replace_person_trajectory,
    upsert_levels,
)
from seniority_v2 import (
    LEVELS,
    MANAGER_INDEX,
    SENIOR_INDEX,
    HAIKU_MODEL,
    classify_level_keyword,
    classify_levels,
    level_index,
    normalize,
)

# Ladder generation. Bump when the ladder/prompt changes so the cache is
# invalidated and the whole cohort re-classifies cleanly under the new rules.
#   v1: initial cross-industry ladder.
#   v2: institutional asset-owner reading (pension/endowment "Director" = senior).
LEVEL_VERSION = 2


@dataclass(frozen=True)
class RoleRecord:
    """One role on a person's timeline, dates merged across sources. `title` and
    `employer` keep original casing (public-facing); classification reads the
    normalized form."""
    title: str
    employer: str
    start_year: int | None
    end_year: int | None
    is_current: bool


@dataclass(frozen=True)
class PersonLevels:
    peak_level: str
    reached_manager: bool
    reached_senior: bool
    years_to_manager: int | None
    years_to_senior: int | None


def compute_person_levels(
    roles: list[tuple[str, str, int | None]],
    grad_year: int | None,
    label_map: dict[tuple[str, str], str],
) -> PersonLevels:
    """Pure: fold a person's roles into a peak rung + two thresholds + two
    velocities. Non-title roles are dropped from the spine entirely. Years are
    None without a grad year or a dated qualifying role; clamped at 0 so a
    pre-graduation senior title can't go negative."""
    ranked: list[tuple[int, int | None]] = []  # (level_index, start_year)
    for title, employer, start in roles:
        lvl = label_map.get((normalize(title), normalize(employer)))
        idx = level_index(lvl) if lvl else None
        if idx is not None:
            ranked.append((idx, start))

    if not ranked:
        return PersonLevels("", False, False, None, None)

    peak = max(i for i, _ in ranked)

    def years_to(min_idx: int) -> int | None:
        if grad_year is None:
            return None
        starts = [s for i, s in ranked if i >= min_idx and s is not None]
        return max(0, min(starts) - grad_year) if starts else None

    return PersonLevels(
        peak_level=LEVELS[peak],
        reached_manager=peak >= MANAGER_INDEX,
        reached_senior=peak >= SENIOR_INDEX,
        years_to_manager=years_to(MANAGER_INDEX),
        years_to_senior=years_to(SENIOR_INDEX),
    )


def _claims_for(conn: sqlite3.Connection, person_id: int) -> list[ClaimRow]:
    rows = conn.execute(
        "SELECT claim_type, value, source_url, quote, confidence, extraction_method "
        "FROM claims WHERE person_id = ?",
        (person_id,),
    ).fetchall()
    return [
        ClaimRow(r["claim_type"], r["value"], r["source_url"], r["quote"],
                 r["confidence"], r["extraction_method"])
        for r in rows
    ]


def _role_records_for_person(
    conn: sqlite3.Connection, person_id: int
) -> list[RoleRecord]:
    """Merged, time-ordered roles from career_history claims AND person_company.
    De-duped on (title_norm, employer_norm): the earliest start, the latest end
    (None = ongoing wins), is_current OR-ed. Ordered by start year (undated
    last). Original casing kept for display."""
    merged: dict[tuple[str, str], dict] = {}

    def add(title: str, employer: str, start: int | None,
            end: int | None, is_current: bool) -> None:
        if not normalize(title):
            return
        key = (normalize(title), normalize(employer))
        cur = merged.get(key)
        if cur is None:
            merged[key] = {
                "title": title.strip(), "employer": (employer or "").strip(),
                "start": start, "end": end, "current": is_current,
            }
            return
        if start is not None and (cur["start"] is None or start < cur["start"]):
            cur["start"] = start
        # Ongoing (end None) beats any dated end; otherwise keep the latest end.
        if is_current or end is None:
            cur["end"] = None
        elif cur["end"] is not None:
            cur["end"] = max(cur["end"], end)
        cur["current"] = cur["current"] or is_current
        if not cur["employer"] and employer:
            cur["employer"] = employer.strip()

    for e in career_entries(_claims_for(conn, person_id)):
        add(e.title, e.company, e.start_year, e.end_year, e.end_year is None and e.start_year is not None)
    for r in conn.execute(
        "SELECT title, company_name, start_year, end_year, is_current "
        "FROM person_company WHERE person_id = ? AND title <> ''",
        (person_id,),
    ):
        add(r["title"], r["company_name"], r["start_year"], r["end_year"], bool(r["is_current"]))

    records = [
        RoleRecord(m["title"], m["employer"], m["start"], m["end"], m["current"])
        for m in merged.values()
    ]
    records = _consolidate(records)
    # Timeline order: by start year ascending, undated roles last.
    records.sort(key=lambda r: (r.start_year is None, r.start_year or 0))
    return records


_FILLER = frozenset({"of", "the", "and", "for", "a", "an", "to", "in"})


def _title_key(title: str) -> str:
    """Loose grouping key: lowercase, strip punctuation + filler words. Collapses
    'EVP of Asset Management' and 'EVP - Asset Management' to the same key so
    punctuation-only variants of one role merge. Used ONLY for grouping; the
    displayed title keeps its original form."""
    import re as _re
    words = _re.sub(r"[^a-z0-9]+", " ", title.lower()).split()
    return " ".join(w for w in words if w not in _FILLER)


def _employers_compatible(a: str, b: str) -> bool:
    """Same role split across sources when one side has no employer, or one
    employer string contains the other ('stephens' vs 'stephens inc')."""
    a, b = normalize(a), normalize(b)
    return not a or not b or a in b or b in a


def _merge_records(rich: RoleRecord, other: RoleRecord) -> RoleRecord:
    """Fold `other`'s dates into `rich`. CRUCIAL: keep rich's (title, employer)
    AS A PAIR — that pair was classified, so its cache lookup will hit. Mixing a
    title from one record with an employer from another could form a pair that
    was never classified and would blank the level. Only dates/current merge."""
    start = min([s for s in (rich.start_year, other.start_year) if s is not None], default=None)
    is_current = rich.is_current or other.is_current
    if is_current or rich.end_year is None or other.end_year is None:
        end = None
    else:
        end = max(rich.end_year, other.end_year)
    return RoleRecord(rich.title, rich.employer, start, end, is_current)


def _consolidate(records: list[RoleRecord]) -> list[RoleRecord]:
    """Collapse near-duplicate roles (identical normalized title, compatible
    employer) into one timeline entry. Same title at genuinely different
    employers is preserved — only compatible employers merge."""
    by_title: dict[str, list[RoleRecord]] = {}
    for r in records:
        by_title.setdefault(_title_key(r.title), []).append(r)

    out: list[RoleRecord] = []
    for group in by_title.values():
        # Richer employer first, so merges fold into the most complete record.
        kept: list[RoleRecord] = []
        for r in sorted(group, key=lambda x: len(normalize(x.employer)), reverse=True):
            for i, k in enumerate(kept):
                if _employers_compatible(k.employer, r.employer):
                    kept[i] = _merge_records(k, r)
                    break
            else:
                kept.append(r)
        out.extend(kept)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Reclassify cohort onto seniority_v2.")
    ap.add_argument("--db", default="data/titans.db")
    ap.add_argument("--dry-run", action="store_true", help="compute + report, no writes")
    ap.add_argument("--no-llm", action="store_true", help="keyword fallback only, no Haiku")
    ap.add_argument("--version", type=int, default=LEVEL_VERSION)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    init_person_insights_schema(conn)
    init_role_level_schema(conn)

    people = conn.execute(
        "SELECT person_id, grad_year, current_sector, first_sector, "
        "reached_md, years_to_md FROM person_insights"
    ).fetchall()

    # 1. Gather roles + sector hint per person.
    person_records: dict[int, list[RoleRecord]] = {}
    all_roles: list[tuple[str, str, str]] = []  # (title, employer, sector)
    for p in people:
        pid = p["person_id"]
        records = _role_records_for_person(conn, pid)
        person_records[pid] = records
        sec = p["current_sector"] or p["first_sector"] or ""
        for r in records:
            all_roles.append((r.title, r.employer, sec))

    # 2. Classify distinct roles — cache first, Haiku only for misses.
    cached = load_cached(conn, args.version)
    distinct = {(normalize(t), normalize(e)) for t, e, _ in all_roles if normalize(t)}
    misses = sorted(distinct - set(cached))
    print(f"roles: {len(distinct)} distinct · {len(cached)} cached · {len(misses)} to classify")

    label_map: dict[tuple[str, str], str] = dict(cached)
    new_rows: list[tuple[str, str, str, str, str, str]] = []
    if misses:
        # sector hint per missing pair (first seen)
        hint = {}
        for t, e, s in all_roles:
            k = (normalize(t), normalize(e))
            if k in set(misses) and (k not in hint or (not hint[k] and normalize(s))):
                hint[k] = normalize(s)
        if args.no_llm:
            for (t, e) in misses:
                lvl = classify_level_keyword(t, e)
                label_map[(t, e)] = lvl
                new_rows.append((t, e, lvl, "keyword", "", hint.get((t, e), "")))
        else:
            client = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))
            triples = [(t, e, hint.get((t, e), "")) for (t, e) in misses]
            result = classify_levels(client, triples)
            kw = {(t, e): classify_level_keyword(t, e) for (t, e) in misses}
            for (t, e) in misses:
                lvl = result.labels.get((t, e), kw[(t, e)])
                src = "haiku" if result.labels.get((t, e)) == lvl else "keyword"
                label_map[(t, e)] = lvl
                new_rows.append((t, e, lvl, src, HAIKU_MODEL, hint.get((t, e), "")))
            print(f"Haiku: {result.input_tokens} in / {result.output_tokens} out tokens")
        if not args.dry_run:
            upsert_levels(conn, args.version, new_rows)
            conn.commit()

    # 3+4. Per-person compute + write + collect for the report.
    model_stamp = "keyword" if args.no_llm else HAIKU_MODEL
    new_reached_senior = new_reached_mgr = 0
    ytm_new: list[int] = []
    yts_new: list[int] = []
    peak_dist: dict[str, int] = {}
    for p in people:
        pid = p["person_id"]
        records = person_records[pid]
        roles = [(r.title, r.employer, r.start_year) for r in records]
        pl = compute_person_levels(roles, p["grad_year"], label_map)
        peak_dist[pl.peak_level or "(none)"] = peak_dist.get(pl.peak_level or "(none)", 0) + 1
        if pl.reached_manager:
            new_reached_mgr += 1
        if pl.reached_senior:
            new_reached_senior += 1
        if pl.years_to_manager is not None:
            ytm_new.append(pl.years_to_manager)
        if pl.years_to_senior is not None:
            yts_new.append(pl.years_to_senior)
        if not args.dry_run:
            conn.execute(
                "UPDATE person_insights SET peak_level=?, reached_manager=?, "
                "reached_senior_leadership=?, years_to_manager=?, "
                "years_to_senior_leadership=?, level_model=?, level_version=? "
                "WHERE person_id=?",
                (pl.peak_level, int(pl.reached_manager), int(pl.reached_senior),
                 pl.years_to_manager, pl.years_to_senior, model_stamp,
                 args.version, pid),
            )
            # Materialize the trajectory: each role + its rung, time-ordered.
            traj = [
                (
                    r.title, r.employer, r.start_year, r.end_year, int(r.is_current),
                    label_map.get((normalize(r.title), normalize(r.employer)), ""),
                    level_index(label_map.get((normalize(r.title), normalize(r.employer)), "")),
                )
                for r in records
            ]
            replace_person_trajectory(conn, pid, args.version, traj)
    if not args.dry_run:
        conn.commit()

    # ---- Report: old vs new ----
    n = len(people)
    old_reached = sum(1 for p in people if p["reached_md"])
    old_ytm = [p["years_to_md"] for p in people if p["years_to_md"] is not None]
    print("\n" + "=" * 64)
    print(f"COHORT: {n} enriched people" + ("   [DRY RUN — no writes]" if args.dry_run else ""))
    print("=" * 64)
    print("\n--- OLD (finance-only 'reached_md', Director/MD bucket) ---")
    print(f"  reached_md           : {old_reached:3d}  ({100*old_reached/n:.1f}%)")
    if old_ytm:
        print(f"  avg years_to_md      : {statistics.mean(old_ytm):.1f}   (median {statistics.median(old_ytm)}, n={len(old_ytm)})")
    print("\n--- NEW (cross-industry, two thresholds) ---")
    print(f"  reached Manager+     : {new_reached_mgr:3d}  ({100*new_reached_mgr/n:.1f}%)")
    if ytm_new:
        print(f"    avg years_to_mgr   : {statistics.mean(ytm_new):.1f}   (median {statistics.median(ytm_new)}, n={len(ytm_new)})")
    print(f"  reached Sr Leadership: {new_reached_senior:3d}  ({100*new_reached_senior/n:.1f}%)")
    if yts_new:
        print(f"    avg years_to_senior: {statistics.mean(yts_new):.1f}   (median {statistics.median(yts_new)}, n={len(yts_new)})")
    print("\n--- PEAK RUNG DISTRIBUTION ---")
    order = list(LEVELS) + ["(none)"]
    for lvl in order:
        c = peak_dist.get(lvl, 0)
        if c:
            print(f"  {c:3d}  ({100*c/n:5.1f}%)  {lvl}")
    conn.close()


if __name__ == "__main__":
    main()
