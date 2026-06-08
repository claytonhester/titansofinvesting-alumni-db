"""Perplexity Agent API adapter (POST /v1/agent).

A different product from the plain /search endpoint (`perplexity_enrich.py`).
The Agent API runs an LLM that can call tools — `people_search`, `web_search`,
`fetch_url`, `finance_search` — and reason over the results itself, returning a
synthesized answer. We use it to ask one question per person: "find public pages
that are confidently about THIS person (name + employer + city), not a namesake,"
and have the agent return structured mentions directly.

Billing is fundamentally different from /search:
  - /search        = flat ~$0.005 per request, no token charge.
  - /v1/agent      = model tokens (provider rates, no markup) + per-tool fees
                     (people_search $0.005, web_search $0.005, fetch_url $0.0005...).

So one Agent call can invoke several tools and burn input/output tokens — the
real cost is only knowable per call, which is why the bake-off reads
`usage.cost.total_cost` straight from the response instead of estimating.

Never raises: any failure (auth, network, malformed JSON, schema drift) returns
an empty result with whatever cost the API reported, so a bulk loop keeps going.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

import httpx

from news_score import has_meaningful_employer

PERPLEXITY_AGENT_URL = "https://api.perplexity.ai/v1/agent"
EXTRACTION_METHOD = "perplexity-agent"

# A small, cheap default model keeps the token side of the bill low while still
# being capable enough to do the identity reasoning. Override per call.
DEFAULT_MODEL = "openai/gpt-5-mini"

# Structured output: force the agent to return a clean list of identity-judged
# mentions so we get the same ClaimRow shape as the Search+Haiku path.
_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "person_mentions",
        "schema": {
            "type": "object",
            "properties": {
                "mentions": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "url": {"type": "string"},
                            "snippet": {"type": "string"},
                            "is_this_person": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                        "required": [
                            "title",
                            "url",
                            "snippet",
                            "is_this_person",
                            "reason",
                        ],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["mentions"],
            "additionalProperties": False,
        },
    },
}


@dataclass(frozen=True)
class AgentMention:
    title: str
    url: str
    snippet: str
    is_this_person: bool
    reason: str


@dataclass(frozen=True)
class AgentResult:
    """One Agent call's outcome. `cost_usd` and `tool_calls` come straight from
    the API's reported usage so the bake-off numbers are real, not estimated."""

    mentions: tuple[AgentMention, ...]
    cost_usd: float
    input_tokens: int
    output_tokens: int
    tool_calls: dict[str, int] = field(default_factory=dict)
    raw_output_chars: int = 0
    error: str | None = None

    @property
    def confirmed(self) -> tuple[AgentMention, ...]:
        return tuple(m for m in self.mentions if m.is_this_person)


_EMPTY = AgentResult(mentions=(), cost_usd=0.0, input_tokens=0, output_tokens=0)


def build_prompt(name: str, employer: str | None, city: str | None) -> str:
    """Instruct the agent to find and identity-verify public pages about the
    person, mirroring the Search+Haiku task so the comparison is apples-to-apples."""
    lines = [
        f"Find public web pages that are about this specific person: {name.strip()}.",
        "They are an alumnus of a Texas university finance/investing program "
        "(Titans of Investing) and work in finance/investing/business.",
    ]
    if has_meaningful_employer(employer):
        lines.append(f"Known employer on record: {employer.strip()} (may be outdated).")
    if city and city.strip():
        lines.append(f"City on record: {city.strip()}.")
    lines.append(
        "Use people_search and web_search. Return company bio pages, leadership "
        "listings, regulatory filings (e.g. FINRA BrokerCheck), professional "
        "profiles, interviews, and press. For EACH result set is_this_person=true "
        "only if you are confident it is THIS person and not a namesake who merely "
        "shares the name; otherwise set it false with a short reason."
    )
    return "\n".join(lines)


def _extract_mentions(body: dict) -> list[AgentMention]:
    """Pull the structured mention list out of the agent's message output. The
    JSON lives in a message OutputItem's content text part."""
    texts: list[str] = []
    for item in body.get("output") or []:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for part in item.get("content") or []:
            if isinstance(part, dict) and part.get("text"):
                texts.append(str(part["text"]))
    blob = "\n".join(texts).strip()
    if not blob:
        return []
    # The model may wrap JSON in prose or fences despite response_format; be lenient.
    parsed = _loads_lenient(blob)
    rows: list[AgentMention] = []
    for m in (parsed or {}).get("mentions") or []:
        if not isinstance(m, dict):
            continue
        title = str(m.get("title") or "").strip()
        url = str(m.get("url") or "").strip()
        if not title or not url:
            continue
        rows.append(
            AgentMention(
                title=title,
                url=url,
                snippet=str(m.get("snippet") or "").strip(),
                is_this_person=bool(m.get("is_this_person")),
                reason=str(m.get("reason") or "").strip(),
            )
        )
    return rows


def _loads_lenient(blob: str) -> dict | None:
    try:
        return json.loads(blob)
    except Exception:
        pass
    # Strip code fences / surrounding prose: grab the outermost {...}.
    start, end = blob.find("{"), blob.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(blob[start : end + 1])
        except Exception:
            return None
    return None


def _tool_calls(usage: dict) -> dict[str, int]:
    details = usage.get("tool_calls_details") or {}
    out: dict[str, int] = {}
    if isinstance(details, dict):
        for name, info in details.items():
            if isinstance(info, dict):
                out[name] = int(info.get("invocation") or info.get("invocations") or 0)
            elif isinstance(info, (int, float)):
                out[name] = int(info)
    return out


def run_agent(
    client: httpx.Client,
    api_key: str,
    name: str,
    *,
    employer: str | None = None,
    city: str | None = None,
    model: str = DEFAULT_MODEL,
    max_steps: int = 6,
    tools: tuple[str, ...] = ("people_search", "web_search"),
    attempts: int = 3,
    backoff_base: float = 1.5,
) -> AgentResult:
    """Run one Agent call for a person and return identity-judged mentions plus
    the API-reported cost. Never raises."""
    if not api_key or not name.strip():
        return _EMPTY
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "input": build_prompt(name, employer, city),
        "model": model,
        "max_steps": max_steps,
        "tools": [{"type": t} for t in tools],
        "response_format": _RESPONSE_SCHEMA,
    }
    for attempt in range(attempts):
        try:
            resp = client.post(PERPLEXITY_AGENT_URL, headers=headers, json=payload, timeout=120.0)
        except Exception as exc:
            if attempt == attempts - 1:
                return AgentResult((), 0.0, 0, 0, error=f"network: {exc}")
            time.sleep(backoff_base ** attempt)
            continue

        if resp.status_code == 200:
            try:
                body = resp.json()
            except Exception as exc:
                return AgentResult((), 0.0, 0, 0, error=f"bad-json: {exc}")
            usage = body.get("usage") or {}
            cost = usage.get("cost") or {}
            mentions = _extract_mentions(body)
            return AgentResult(
                mentions=tuple(mentions),
                cost_usd=float(cost.get("total_cost") or 0.0),
                input_tokens=int(usage.get("input_tokens") or 0),
                output_tokens=int(usage.get("output_tokens") or 0),
                tool_calls=_tool_calls(usage),
                raw_output_chars=sum(len(m.title) + len(m.snippet) for m in mentions),
            )
        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == attempts - 1:
                return AgentResult((), 0.0, 0, 0, error=f"http {resp.status_code}")
            time.sleep(backoff_base ** attempt)
            continue
        # 4xx other than 429: retrying won't help. Capture body for diagnosis.
        return AgentResult((), 0.0, 0, 0, error=f"http {resp.status_code}: {resp.text[:300]}")
    return _EMPTY
