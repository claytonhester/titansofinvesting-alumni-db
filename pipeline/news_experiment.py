"""A/B harness for news-discovery strategies (read-only, no DB writes).

Goal: on a small, eyeball-able sample of alumni — who we only know by name + an
old employer — try several query strategies across GNews and GDELT, score each
hit for precision (news_score), and print a comparison so we can SEE what works
before committing to a 1,000-person run.

It never writes to the claims table; results go to stdout and a markdown report.

    # 12-person sample, both sources (default)
    python news_experiment.py

    # bigger sample, GDELT only (free, no GNews quota used), wider window
    python news_experiment.py --limit 25 --sources gdelt --timespan 36m

GNews note: each GNews strategy issues one request per person against your daily
quota (free tier ~100/day). The harness prints the request count up front.
GDELT is free but rate-limited to ~1 req / 5s, so it is paced (~6s/call).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from anthropic import Anthropic

from config import DB_PATH
from gdelt_enrich import fetch_gdelt
from gnews_enrich import fetch_news
from news_score import MentionScore, normalize_domain, score_mention
from news_verify import Candidate, verify_hits
from perplexity_enrich import fetch_perplexity

# Each strategy: (key, source, use_employer, human label).
_STRATEGIES = [
    ("gnews_name", "gnews", False, "GNews — name only (current baseline)"),
    ("gnews_name_employer", "gnews", True, "GNews — name AND employer"),
    ("gdelt_name", "gdelt", False, "GDELT — name only"),
    ("gdelt_name_employer", "gdelt", True, "GDELT — name AND employer"),
    ("pplx_name", "perplexity", False, "Perplexity — name only"),
    ("pplx_name_employer", "perplexity", True, "Perplexity — name + employer"),
]

# Sources that require an API key (used to skip strategies when a key is unset).
_KEYED_SOURCES = {"gnews", "perplexity"}
# Rough per-request dollar cost (informational); GDELT is free, GNews is a flat
# subscription so its marginal cost is ~0.
_USD_PER_REQUEST = {"perplexity": 0.005}


@dataclass(frozen=True)
class SamplePerson:
    id: int
    full_name: str
    employer: str
    city: str


@dataclass
class Hit:
    title: str
    url: str
    domain: str
    date: str
    score: MentionScore
    snippet: str = ""
    verified: bool | None = None  # None = not run; True/False = LLM verdict


@dataclass
class StrategyStats:
    key: str
    label: str
    source: str
    people: int = 0
    people_with_hits: int = 0
    people_with_confident: int = 0
    people_with_plausible: int = 0
    people_with_verified: int = 0
    total_hits: int = 0
    confident_hits: int = 0
    plausible_hits: int = 0
    verified_hits: int = 0
    requests: int = 0
    verified_run: bool = False
    per_person: dict[int, list[Hit]] = field(default_factory=dict)

    def coverage_pct(self) -> int:
        return round(100 * self.people_with_hits / self.people) if self.people else 0

    def confident_pct(self) -> int:
        return round(100 * self.people_with_confident / self.people) if self.people else 0

    def plausible_pct(self) -> int:
        return round(100 * self.people_with_plausible / self.people) if self.people else 0

    def verified_pct(self) -> int:
        return round(100 * self.people_with_verified / self.people) if self.people else 0


def even_sample(ids: list[int], limit: int, offset: int = 0) -> list[int]:
    """Pick up to ``limit`` ids spread evenly across the list (so the sample spans
    cohorts, not just the first one). Deterministic for a given input."""
    pool = ids[offset:]
    if limit <= 0 or not pool:
        return []
    if limit >= len(pool):
        return pool
    step = len(pool) / limit
    return [pool[int(i * step)] for i in range(limit)]


def load_sample(conn: sqlite3.Connection, limit: int, offset: int) -> list[SamplePerson]:
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, full_name, initial_company, city FROM people "
        "WHERE needs_review = 0 AND initial_company NOT IN ('', '(unknown)') "
        "ORDER BY id"
    ).fetchall()
    by_id = {r["id"]: r for r in rows}
    chosen = even_sample([r["id"] for r in rows], limit, offset)
    return [
        SamplePerson(
            id=by_id[i]["id"],
            full_name=by_id[i]["full_name"],
            employer=by_id[i]["initial_company"],
            city=by_id[i]["city"],
        )
        for i in chosen
    ]


def _gnews_hits(
    http: httpx.Client, key: str, person: SamplePerson, use_employer: bool, max_articles: int
) -> tuple[list[Hit], int]:
    employer = person.employer if use_employer else None
    result = fetch_news(http, key, person.full_name, employer=employer, max_articles=max_articles)
    hits: list[Hit] = []
    for row in result.claim_rows:
        # ClaimRow.value is "YYYY-MM-DD — Headline" (or just the headline).
        date, sep, headline = row.value.partition(" — ")
        if not sep:
            date, headline = "", row.value
        domain = normalize_domain(row.source_url)
        score = score_mention(
            name=person.full_name, employer=person.employer,
            title=headline, snippet=row.quote, domain=domain,
        )
        hits.append(Hit(title=headline, url=row.source_url, domain=domain, date=date,
                        score=score, snippet=row.quote))
    return hits, result.request_count


def _gdelt_hits(
    http: httpx.Client, person: SamplePerson, use_employer: bool, max_articles: int, timespan: str
) -> list[Hit]:
    employer = person.employer if use_employer else None
    articles = fetch_gdelt(
        http, person.full_name, employer=employer, max_records=max_articles, timespan=timespan
    )
    hits: list[Hit] = []
    for art in articles:
        domain = normalize_domain(art.domain or art.url)
        score = score_mention(
            name=person.full_name, employer=person.employer,
            title=art.title, snippet="", domain=domain,
        )
        hits.append(Hit(title=art.title, url=art.url, domain=domain, date=art.seendate[:8], score=score))
    return hits


def _perplexity_hits(
    http: httpx.Client, key: str, person: SamplePerson, use_employer: bool, max_articles: int
) -> list[Hit]:
    employer = person.employer if use_employer else None
    results = fetch_perplexity(http, key, person.full_name, employer=employer, max_results=max_articles)
    hits: list[Hit] = []
    for r in results:
        domain = normalize_domain(r.url)
        score = score_mention(
            name=person.full_name, employer=person.employer,
            title=r.title, snippet=r.snippet, domain=domain,
        )
        hits.append(Hit(title=r.title, url=r.url, domain=domain, date=r.date[:10],
                        score=score, snippet=r.snippet))
    return hits


def _record(stats: StrategyStats, person_id: int, hits: list[Hit]) -> None:
    stats.people += 1
    stats.per_person[person_id] = hits
    if hits:
        stats.people_with_hits += 1
        stats.total_hits += len(hits)
    confident = [h for h in hits if h.score.confident]
    if confident:
        stats.people_with_confident += 1
        stats.confident_hits += len(confident)
    plausible = [h for h in hits if h.score.plausible]
    if plausible:
        stats.people_with_plausible += 1
        stats.plausible_hits += len(plausible)
    verified = [h for h in hits if h.verified is True]
    if verified:
        stats.people_with_verified += 1
        stats.verified_hits += len(verified)


def run_experiment(
    sample: list[SamplePerson],
    sources: set[str],
    keys: dict[str, str | None],
    *,
    max_articles: int,
    timespan: str,
    gdelt_pace: float,
    verifier: "Anthropic | None" = None,
    drop_aggregators: bool = False,
) -> dict[str, StrategyStats]:
    def available(source: str) -> bool:
        return source not in _KEYED_SOURCES or bool(keys.get(source))

    active = [s for s in _STRATEGIES if s[1] in sources and available(s[1])]
    stats = {s[0]: StrategyStats(key=s[0], label=s[3], source=s[1], verified_run=bool(verifier))
             for s in active}

    def count(source: str) -> int:
        return sum(1 for s in active if s[1] == source) * len(sample)

    pplx_cost = count("perplexity") * _USD_PER_REQUEST["perplexity"]
    print(f"Sample: {len(sample)} people | strategies: {len(active)}")
    print(f"GNews requests (daily quota): {count('gnews')}")
    print(f"GDELT requests (free, paced ~{gdelt_pace:.0f}s): {count('gdelt')}"
          f"  ~{round(count('gdelt') * gdelt_pace / 60)} min")
    print(f"Perplexity requests (~${_USD_PER_REQUEST['perplexity']}/req): "
          f"{count('perplexity')}  ~${pplx_cost:.2f}\n")

    with httpx.Client(timeout=30.0) as http:
        for idx, person in enumerate(sample, 1):
            print(f"[{idx}/{len(sample)}] {person.full_name}  ·  {person.employer}")
            for key, source, use_emp, _label in active:
                if source == "gnews":
                    hits, reqs = _gnews_hits(http, keys.get("gnews"), person, use_emp, max_articles)
                    stats[key].requests += reqs
                elif source == "perplexity":
                    hits = _perplexity_hits(http, keys.get("perplexity"), person, use_emp, max_articles)
                    stats[key].requests += 1
                else:
                    time.sleep(gdelt_pace)  # respect GDELT's ~1 req / 5s limit
                    hits = _gdelt_hits(http, person, use_emp, max_articles, timespan)
                    stats[key].requests += 1
                if drop_aggregators:
                    hits = [h for h in hits if not h.score.aggregator_domain]
                if verifier is not None and hits:
                    _verify(verifier, person, hits)
                _record(stats[key], person.id, hits)
                conf = sum(1 for h in hits if h.score.confident)
                ver = sum(1 for h in hits if h.verified is True)
                tail = f", {ver} verified" if verifier is not None else ""
                print(f"    {key:<22} {len(hits)} hits, {conf} confident{tail}")
    return stats


def _verify(verifier: "Anthropic", person: SamplePerson, hits: list[Hit]) -> None:
    """Run the LLM identity check over a person's hits and stamp each .verified."""
    candidates = [Candidate(title=h.title, snippet=h.snippet, domain=h.domain) for h in hits]
    verdicts = verify_hits(verifier, person.full_name, person.employer, person.city, candidates)
    for hit, verdict in zip(hits, verdicts):
        hit.verified = verdict.is_match


def _summary_table(stats: dict[str, StrategyStats]) -> str:
    verified_run = any(s.verified_run for s in stats.values())
    ver_head = " LLM-verified |" if verified_run else ""
    ver_sep = "--------------|" if verified_run else ""
    head = f"| {'Strategy':<34} | People | Coverage | Confident |{ver_head} Hits |"
    sep = "|" + "-" * 36 + "|--------|----------|-----------|" + ver_sep + "------|"
    lines = [head, sep]
    for s in stats.values():
        ver_cell = f" {s.people_with_verified:>3} ({s.verified_pct():>3}%) |" if verified_run else ""
        lines.append(
            f"| {s.label:<34} | {s.people:>6} | {s.people_with_hits:>3} ({s.coverage_pct():>3}%) "
            f"| {s.people_with_confident:>3} ({s.confident_pct():>3}%) |{ver_cell} {s.total_hits:>4} |"
        )
    return "\n".join(lines)


def _detail_block(sample: list[SamplePerson], stats: dict[str, StrategyStats]) -> str:
    out: list[str] = []
    for person in sample:
        out.append(f"\n### {person.full_name} — {person.employer}")
        for s in stats.values():
            hits = s.per_person.get(person.id, [])
            if not hits:
                out.append(f"- _{s.key}_: (none)")
                continue
            out.append(f"- _{s.key}_:")
            for h in hits:
                if h.verified is True:
                    mark = "✅"
                elif h.verified is False:
                    mark = "❌"
                elif h.score.confident:
                    mark = "•"
                else:
                    mark = "  "
                out.append(f"    {mark} [{h.date}] {h.domain} — {h.title[:90]}")
    return "\n".join(out)


def write_report(path: Path, sample: list[SamplePerson], stats: dict[str, StrategyStats]) -> None:
    body = [
        "# News discovery — A/B experiment",
        "",
        f"Sample of {len(sample)} alumni (name + initial employer only). "
        "Hits scored by news_score; ✅ = passed the precision filter "
        "(employer co-mention, or finance outlet naming them in the headline).",
        "",
        "## Summary",
        "",
        _summary_table(stats),
        "",
        "## Per-person hits",
        _detail_block(sample, stats),
        "",
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(body), encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="news-experiment", description=__doc__)
    p.add_argument("--limit", type=int, default=12, help="sample size (default 12)")
    p.add_argument("--offset", type=int, default=0, help="skip the first N eligible people")
    p.add_argument("--sources", default="gnews,gdelt,perplexity",
                   help="comma list: gnews,gdelt,perplexity")
    p.add_argument("--max-articles", type=int, default=5)
    p.add_argument("--timespan", default="24m", help="GDELT lookback, e.g. 24m, 36m, 5y")
    p.add_argument("--gdelt-pace", type=float, default=6.0, help="seconds between GDELT calls")
    p.add_argument("--verify", action="store_true",
                   help="run the Claude identity check on each hit (needs ANTHROPIC_API_KEY)")
    p.add_argument("--drop-aggregators", action="store_true",
                   help="discard people-search/data-broker domains before scoring/verifying")
    p.add_argument("--out", type=Path, default=DB_PATH.parent / "news_experiment.md")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sources = {s.strip().lower() for s in args.sources.split(",") if s.strip()}
    keys = {
        "gnews": os.getenv("GNEWS_API_KEY"),
        "perplexity": os.getenv("PERPLEXITY_API_KEY"),
    }
    for src in _KEYED_SOURCES:
        if src in sources and not keys.get(src):
            print(f"(no key for {src} — skipping its strategies)")

    verifier = None
    if args.verify:
        anthropic_key = os.getenv("ANTHROPIC_API_KEY")
        if not anthropic_key:
            print("(--verify set but no ANTHROPIC_API_KEY — skipping verification)")
        else:
            verifier = Anthropic(api_key=anthropic_key)

    conn = sqlite3.connect(DB_PATH)
    try:
        sample = load_sample(conn, args.limit, args.offset)
    finally:
        conn.close()
    if not sample:
        print("No eligible people found.")
        return 1

    stats = run_experiment(
        sample, sources, keys,
        max_articles=args.max_articles, timespan=args.timespan, gdelt_pace=args.gdelt_pace,
        verifier=verifier, drop_aggregators=args.drop_aggregators,
    )
    print("\n" + _summary_table(stats) + "\n")
    write_report(args.out, sample, stats)
    print(f"Full per-person report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
