"""Central configuration. Loads .env; secrets are optional until Stage 2."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PIPELINE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PIPELINE_DIR.parent
DATA_DIR = PIPELINE_DIR / "data"
SNAPSHOT_DIR = DATA_DIR / "snapshots"
DB_PATH = DATA_DIR / "titans.db"

DIRECTORY_URL = "https://www.titansofinvesting.org/titans-class-directory"
USER_AGENT = "Mozilla/5.0 (compatible; titans-research/1.0)"

# override=True so the project's .env wins over an empty/stale ambient var
# (some shells inject an empty ANTHROPIC_API_KEY that would otherwise shadow it).
load_dotenv(REPO_ROOT / ".env", override=True)


@dataclass(frozen=True)
class Secrets:
    """Stage-2 API keys. Empty in Stage 1 — never required to scrape the directory."""

    firecrawl_api_key: str | None
    anthropic_api_key: str | None


def load_secrets() -> Secrets:
    return Secrets(
        firecrawl_api_key=os.getenv("FIRECRAWL_API_KEY") or None,
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY") or None,
    )


def require_key(name: str) -> str:
    """Fail fast when a Stage-2 step actually needs a key."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"{name} is required for this step but is not set. "
            f"Add it to {REPO_ROOT / '.env'} (see .env.example)."
        )
    return value
