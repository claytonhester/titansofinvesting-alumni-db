"""Domain models for the pipeline. Stage 1 only needs PersonRecord."""
from __future__ import annotations

import re
import unicodedata

from pydantic import BaseModel, field_validator

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """URL slug for a person. Transliterates accents first (NFKD) so
    'González' -> 'gonzalez' rather than the lossy 'gonz-lez'."""
    ascii_name = (
        unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    )
    return _SLUG_STRIP.sub("-", ascii_name.strip().lower()).strip("-")


class PersonRecord(BaseModel):
    """One alumnus as parsed from the public directory. The directory gives
    only name/company/city; cohort and school come from the section heading."""

    full_name: str
    name_slug: str
    titan_class: int
    school: str
    initial_company: str
    city: str
    source_url: str
    # True when the raw entry did not split cleanly into name/company/city,
    # so a human should eyeball it before it is trusted downstream.
    needs_review: bool = False
    raw_entry: str

    @field_validator("full_name", "initial_company", "city", "school")
    @classmethod
    def _not_blank(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("must not be blank")
        return v
