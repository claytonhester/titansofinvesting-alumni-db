"""Command-line entrypoint for the Titans pipeline.

Stage 1 exposes a single subcommand: `ingest`, which scrapes the public
directory and populates SQLite. Later stages add their own subcommands.

    python cli.py ingest                 # fetch live and ingest
    python cli.py ingest --html snap.html  # re-parse a saved snapshot
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from config import DB_PATH, DIRECTORY_URL
from phase1_ingest import ingest


def _cmd_ingest(args: argparse.Namespace) -> int:
    html = Path(args.html).read_text(encoding="utf-8") if args.html else None
    result = ingest(url=args.url, html=html)
    print(f"Parsed:        {result.parsed} person rows")
    print(f"Needs review:  {result.needs_review}")
    print(f"Snapshot:      {result.snapshot_path}")
    print(f"Total in DB:   {result.total_in_db} ({DB_PATH})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="titans", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    ingest_p = sub.add_parser("ingest", help="Scrape the public directory into SQLite")
    ingest_p.add_argument("--url", default=DIRECTORY_URL, help="Directory URL to fetch")
    ingest_p.add_argument(
        "--html",
        default=None,
        help="Path to a saved HTML snapshot to re-parse instead of fetching",
    )
    ingest_p.set_defaults(func=_cmd_ingest)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
