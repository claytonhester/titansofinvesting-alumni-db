"""Phase 2 prototype: measure the TRUE per-person enrichment cost.

Runs the full Firecrawl-first pipeline (search+scrape -> single Haiku
structuring call) over a small sample of real alumni, dumps the raw evidence
and the structured profile, and reports actual Firecrawl credits + Claude
tokens so we can extrapolate the 1,056-person run before committing to it.

    python phase2_discover.py --limit 5            # random 5 from DB
    python phase2_discover.py --name "Jane Doe"    # one specific person
    python phase2_discover.py --limit 5 --dump out # also write raw markdown
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from anthropic import Anthropic
from firecrawl import Firecrawl

from config import DB_PATH, require_key
from cost_log import (
    USD_PER_CREDIT,
    append_entry,
    build_entry,
    claude_usd,
    remaining_credits,
)
from db import connect
from discovery import DiscoveryResult, discover
from structuring import HAIKU_MODEL, StructuringResult, structure_profile

_FULL_RUN_PEOPLE = 1056


@dataclass(frozen=True)
class PersonRow:
    full_name: str
    initial_company: str
    city: str


def _load_people(limit: int, name: str | None) -> list[PersonRow]:
    with connect(DB_PATH) as conn:
        if name:
            rows = conn.execute(
                "SELECT full_name, initial_company, city FROM people WHERE full_name = ?",
                (name,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT full_name, initial_company, city FROM people "
                "WHERE needs_review = 0 ORDER BY RANDOM() LIMIT ?",
                (limit,),
            ).fetchall()
    return [PersonRow(r["full_name"], r["initial_company"], r["city"]) for r in rows]


def _person_cost_usd(disc: DiscoveryResult, struct: StructuringResult) -> float:
    """Estimated per-person cost. This prototype does NOT run the Sonnet identity
    gate (phase2_enrich.py does), so Claude here is Haiku-only by construction —
    the run-level total below records the authoritative Firecrawl credit delta."""
    fc = disc.credits_spent * USD_PER_CREDIT
    claude = claude_usd(struct.input_tokens, struct.output_tokens, 0, 0)
    return fc + claude


def _dump_evidence(out_dir: Path, disc: DiscoveryResult, struct: StructuringResult) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = disc.full_name.lower().replace(" ", "-")
    (out_dir / f"{slug}.md").write_text(
        "\n\n---\n\n".join(
            f"# {s.title}\n{s.url} (relevance {s.relevance:.2f})\n\n{s.markdown}"
            for s in disc.sources
        ),
        encoding="utf-8",
    )
    (out_dir / f"{slug}.profile.json").write_text(
        json.dumps(struct.profile, indent=2), encoding="utf-8"
    )


def run(limit: int, name: str | None, dump_dir: Path | None) -> int:
    people = _load_people(limit, name)
    if not people:
        print("No matching people in DB.", file=sys.stderr)
        return 1

    firecrawl = Firecrawl(api_key=require_key("FIRECRAWL_API_KEY"))
    anthropic = Anthropic(api_key=require_key("ANTHROPIC_API_KEY"))

    # Authoritative cost: snapshot the live credit meter around the whole run.
    credits_before = remaining_credits(firecrawl)

    total_credits = 0
    total_in_tok = 0
    total_out_tok = 0
    total_usd = 0.0

    for person in people:
        print(f"\n=== {person.full_name} | {person.initial_company} | {person.city} ===")
        disc = discover(firecrawl, person.full_name, person.initial_company, person.city)
        print(f"  sources: {len(disc.sources)}  credits: {disc.credits_spent}")
        for s in disc.sources:
            print(f"    [{s.relevance:.2f}] {s.url}")

        struct = structure_profile(anthropic, person.full_name, disc.sources)
        filled = sum(1 for v in struct.profile.values() if v not in (None, [], {}))
        print(
            f"  structured fields: {filled}  "
            f"tokens in/out: {struct.input_tokens}/{struct.output_tokens}"
        )

        cost = _person_cost_usd(disc, struct)
        print(f"  person cost: ${cost:.4f}")

        total_credits += disc.credits_spent
        total_in_tok += struct.input_tokens
        total_out_tok += struct.output_tokens
        total_usd += cost

        if dump_dir is not None:
            _dump_evidence(dump_dir, disc, struct)

    credits_after = remaining_credits(firecrawl)
    entry = build_entry(
        label=name or f"sample-{len(people)}",
        people=len(people),
        haiku_in=total_in_tok,
        haiku_out=total_out_tok,
        credits_before=credits_before,
        credits_after=credits_after,
        estimated_credits=total_credits,
    )
    append_entry(entry)

    n = len(people)
    # Prefer the measured credit delta for the headline; fall back to the estimate.
    measured_credits = entry.firecrawl_credits
    avg = entry.total_usd / n
    print("\n" + "=" * 56)
    print(f"Sample: {n} people  |  model: {HAIKU_MODEL}")
    label = "estimated" if entry.firecrawl_credits_estimated else "measured"
    print(
        f"Firecrawl credits ({label}): {measured_credits} "
        f"(avg {measured_credits / n:.1f}/person)"
    )
    print(f"Claude tokens in/out: {total_in_tok}/{total_out_tok}")
    print(f"Avg cost/person: ${avg:.4f}  (logged to data/cost_log.jsonl)")
    print(
        f"Extrapolated {_FULL_RUN_PEOPLE}-person run: "
        f"${avg * _FULL_RUN_PEOPLE:.2f}  "
        f"({measured_credits / n * _FULL_RUN_PEOPLE:.0f} Firecrawl credits)"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="phase2-discover", description=__doc__)
    p.add_argument("--limit", type=int, default=5, help="How many random alumni to sample")
    p.add_argument("--name", default=None, help="Profile one specific person by full name")
    p.add_argument("--dump", default=None, help="Directory to write raw markdown + profile JSON")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dump_dir = Path(args.dump) if args.dump else None
    return run(limit=args.limit, name=args.name, dump_dir=dump_dir)


if __name__ == "__main__":
    sys.exit(main())
