"""LLM identity verification for candidate news/web hits.

String matching can't tell our finance alum "Thomas Green" from a Confederate
general or a therapist who shares the name. This asks Claude Haiku to make that
call: given the target person (name, known employer, city — all alumni of a
Texas finance/investing program) and a batch of search results, it judges each
result yes / no / unsure for "is this the same person?".

Batched one call per person (all their hits at once) for cost and so the model
can compare results. Never raises: any failure returns "unsure" for every hit so
the caller falls back to the heuristic score instead of crashing a run.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import Anthropic

from structuring import HAIKU_MODEL

EXTRACTION_METHOD = "claude-haiku-verify"

_SYSTEM = """You decide whether a web search result is about ONE specific \
person (the target) or a different person who merely shares the name (a namesake).

Every target is an alumnus of a Texas university finance/investing program \
(Titans of Investing). For each you get their name, the ONE employer on record \
(which may be outdated, or may actually be the school they attended), and a city.

A result counts ONLY if the page is substantively ABOUT this person — not merely a \
page that happens to name them. Judge each result (title + snippet + source domain):
- "yes" — the page is about the TARGET as an individual AND fits a \
finance/investing/business professional: their bio or profile, an interview or \
Q&A with them, a named leadership/role listing for them, an article focused on \
them or quoting them, or an authoritative record about them personally (e.g. a \
FINRA BrokerCheck record for this person).
- "no" — ANY of: (a) the page is primarily about their COMPANY/employer, not them \
— funding rounds, product launches, "top company" awards, market news — even if \
they are the CEO and named in passing; (b) a public-records or people-search \
DATABASE lookup — salary explorers, government salary directories, data brokers — \
even if the name matches exactly; (c) a clearly DIFFERENT person (different \
profession, historical figure, unrelated field/location); (d) only a common word \
overlaps ("university", "energy", "texas") with no person-level fit.
- "unsure" — genuinely ambiguous; not enough signal to decide.

Be strict: a shared name alone is NOT a match, and the person being merely \
mentioned on a page about their company is NOT a match. When the page is about the \
firm rather than the human, choose "no".

Return ONLY a JSON array, one object per result, in the SAME order:
[{"index": <int>, "verdict": "yes|no|unsure", "reason": "<short>"}]"""


@dataclass(frozen=True)
class Verdict:
    index: int
    verdict: str  # "yes" | "no" | "unsure"
    reason: str

    @property
    def is_match(self) -> bool:
        return self.verdict == "yes"


@dataclass(frozen=True)
class Candidate:
    title: str
    snippet: str
    domain: str


def _build_user(name: str, employer: str, city: str, candidates: list[Candidate]) -> str:
    lines = [
        "Target person:",
        f"  Name: {name}",
        f"  Known employer (may be outdated or a school): {employer or '(unknown)'}",
        f"  City: {city or '(unknown)'}",
        "",
        "Results to judge:",
    ]
    for i, c in enumerate(candidates):
        lines.append(f"[{i}] title: {c.title}")
        if c.snippet:
            lines.append(f"    snippet: {c.snippet[:300]}")
        lines.append(f"    source: {c.domain}")
    return "\n".join(lines)


def _parse_verdicts(text: str, n: int) -> list[Verdict]:
    """Parse the model's JSON array into per-index verdicts. Anything missing or
    malformed becomes 'unsure' so the count of results is always preserved."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    cleaned = cleaned.strip()
    parsed: object = None
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("["), cleaned.rfind("]")
        if 0 <= start < end:
            try:
                parsed = json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                parsed = None

    by_index: dict[int, Verdict] = {}
    if isinstance(parsed, list):
        for item in parsed:
            if not isinstance(item, dict):
                continue
            try:
                idx = int(item.get("index"))
            except (TypeError, ValueError):
                continue
            verdict = str(item.get("verdict", "unsure")).strip().lower()
            if verdict not in ("yes", "no", "unsure"):
                verdict = "unsure"
            by_index[idx] = Verdict(idx, verdict, str(item.get("reason", "")).strip())

    return [by_index.get(i, Verdict(i, "unsure", "no verdict returned")) for i in range(n)]


def verify_hits(
    client: Anthropic,
    name: str,
    employer: str,
    city: str,
    candidates: list[Candidate],
    *,
    model: str = HAIKU_MODEL,
    max_tokens: int = 1024,
) -> list[Verdict]:
    """Judge a person's candidate hits in one call. Returns one Verdict per
    candidate (same order). On any error, all 'unsure'."""
    if not candidates:
        return []
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": _build_user(name, employer, city, candidates)}],
        )
        text = "".join(b.text for b in response.content if b.type == "text")
    except Exception:
        return [Verdict(i, "unsure", "verification call failed") for i in range(len(candidates))]
    return _parse_verdicts(text, len(candidates))
