"""Bake-off probe: Perplexity Sonar (web-grounded LLM w/ citations) as a profile
source — the one Perplexity product we've never tested.

For each person we ask Sonar for a SOURCED career profile (current role, dated work
history, education, notable press) as JSON, plus the response's citation list. We
do NOT trust it blindly — the point is to measure, per person:
  - coverage: did it find a real, specific profile (vs. "couldn't confirm")?
  - citations: are they credible (LinkedIn / firm / news) or aggregator noise?
  - namesake handling: does it flag uncertainty on common names?
  - cost: read from usage.cost when present, else computed from token usage.

Compares sonar-pro across a mix (empties / rich / military) and runs the deeper
sonar-deep-research on the hard empties specifically. Read-only on the DB.
Spends real money (~$0.50-1.00 total).
"""
from __future__ import annotations

import json
import os
import sqlite3
from urllib.parse import urlparse

import httpx
from config import REPO_ROOT
from dotenv import load_dotenv

PPLX_URL = "https://api.perplexity.ai/chat/completions"

# USD per 1M tokens (input, output) + per-request fee (medium search tier, /1k).
PRICING = {
    "sonar-pro": (3.0, 15.0, 0.010),
    "sonar": (1.0, 1.0, 0.008),
    "sonar-deep-research": (2.0, 8.0, 0.0),  # cost read from usage when possible
}

_SYSTEM = (
    "You research ONE specific person for a finance alumni directory and report "
    "only what public web sources support. Cite sources. If you cannot confidently "
    "tell this person apart from a namesake, set found=false and explain. Never "
    "invent roles, dates, or employers."
)


def _user(name: str, employer: str, city: str) -> str:
    return (
        f"Treat '{name}' as ONE person's full name (first + last) — not a place or "
        f"company. Person: {name}. They are an alumnus of Titans of Investing (a Texas "
        f"university finance program) and likely work in finance/investing/business. "
        f"Known employer on record: {employer or '(unknown)'}. City: {city or '(unknown)'}.\n\n"
        "Find: their CURRENT title + employer, full work history with start/end "
        "years, education (degree, school, year), and any notable press/news about "
        "THEM specifically (interviews, awards, deals, appointments).\n\n"
        "Return ONLY this JSON:\n"
        '{"found": bool, "confidence": "high|medium|low", "current_title": str, '
        '"current_employer": str, "location": str, '
        '"career_history": [{"title": str, "company": str, "start": str, "end": str}], '
        '"education": [{"degree": str, "school": str, "year": str}], '
        '"press": [{"headline": str, "url": str}], '
        '"namesake_risk": str}'
    )


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").removeprefix("www.")
    except Exception:
        return ""


def _loads_lenient(blob: str) -> dict | None:
    blob = blob.strip()
    if blob.startswith("```"):
        blob = blob.split("\n", 1)[-1]
        if blob.endswith("```"):
            blob = blob[:-3]
    try:
        return json.loads(blob)
    except Exception:
        s, e = blob.find("{"), blob.rfind("}")
        if 0 <= s < e:
            try:
                return json.loads(blob[s : e + 1])
            except Exception:
                return None
    return None


def _cost(model: str, usage: dict) -> float:
    reported = (usage.get("cost") or {}).get("total_cost") if isinstance(usage.get("cost"), dict) else None
    if isinstance(reported, (int, float)):
        return float(reported)
    pin, pout, req_fee = PRICING.get(model, (3.0, 15.0, 0.010))
    pt = usage.get("prompt_tokens", 0) or 0
    ct = usage.get("completion_tokens", 0) or 0
    cite = usage.get("citation_tokens", 0) or 0
    reason = usage.get("reasoning_tokens", 0) or 0
    searches = usage.get("num_search_queries", 0) or 0
    base = (pt / 1e6) * pin + (ct / 1e6) * pout + req_fee
    # deep-research extras
    base += (cite / 1e6) * 2.0 + (reason / 1e6) * 3.0 + (searches / 1000) * 5.0
    return base


_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "confidence": {"type": "string"},
        "current_title": {"type": "string"},
        "current_employer": {"type": "string"},
        "location": {"type": "string"},
        "career_history": {"type": "array", "items": {"type": "object", "properties": {
            "title": {"type": "string"}, "company": {"type": "string"},
            "start": {"type": "string"}, "end": {"type": "string"}}}},
        "education": {"type": "array", "items": {"type": "object", "properties": {
            "degree": {"type": "string"}, "school": {"type": "string"}, "year": {"type": "string"}}}},
        "press": {"type": "array", "items": {"type": "object", "properties": {
            "headline": {"type": "string"}, "url": {"type": "string"}}}},
        "namesake_risk": {"type": "string"},
    },
    "required": ["found", "confidence", "current_title", "current_employer",
                 "location", "career_history", "education", "press", "namesake_risk"],
}


def query_sonar(http: httpx.Client, key: str, model: str, name: str, employer: str, city: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _user(name, employer, city)},
        ],
        # Enforce the JSON shape — without this, adherence is non-deterministic
        # (the model sometimes dumps found data into a free-text field).
        "response_format": {"type": "json_schema", "json_schema": {"schema": _SCHEMA}},
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        r = http.post(PPLX_URL, json=payload, headers=headers, timeout=180.0)
        r.raise_for_status()
        body = r.json()
    except Exception as exc:
        return {"error": str(exc)[:120], "cost": 0.0}
    content = (body.get("choices") or [{}])[0].get("message", {}).get("content", "")
    citations = body.get("citations") or body.get("search_results") or []
    cite_urls = [c if isinstance(c, str) else (c.get("url") if isinstance(c, dict) else "") for c in citations]
    parsed = _loads_lenient(content) or {}
    return {
        "parsed": parsed,
        "citations": [u for u in cite_urls if u],
        "cost": _cost(model, body.get("usage") or {}),
        "usage": body.get("usage") or {},
    }


def _person(conn, name):
    return conn.execute(
        "SELECT full_name, COALESCE(NULLIF(research_company,''), initial_company) company, city "
        "FROM people WHERE full_name = ? LIMIT 1", (name,),
    ).fetchone()


def _report(label: str, name: str, res: dict) -> float:
    print(f"\n=== {label}: {name} ===")
    if res.get("error"):
        print(f"  ERROR: {res['error']}")
        return res.get("cost", 0.0)
    p = res["parsed"]
    cites = res["citations"]
    hosts = sorted({_host(u) for u in cites if _host(u)})
    print(f"  found={p.get('found')} confidence={p.get('confidence')}  cost=${res['cost']:.4f}")
    print(f"  current: {p.get('current_title','?')} @ {p.get('current_employer','?')} ({p.get('location','?')})")
    print(f"  career={len(p.get('career_history') or [])}  edu={len(p.get('education') or [])}  press={len(p.get('press') or [])}")
    if p.get("namesake_risk"):
        print(f"  namesake_risk: {str(p['namesake_risk'])[:140]}")
    print(f"  {len(cites)} citations: {', '.join(hosts[:8])}")
    for art in (p.get("press") or [])[:4]:
        print(f"     press: {str(art.get('headline',''))[:60]}  [{_host(art.get('url',''))}]")
    return res["cost"]


def main() -> None:
    load_dotenv(REPO_ROOT / ".env", override=True)
    key = os.environ["PERPLEXITY_API_KEY"]
    conn = sqlite3.connect("data/titans.db"); conn.row_factory = sqlite3.Row

    pro = ["Alan Boyd", "Michael Rooney", "Danny Pohlman",
           "Komson Silapachai", "Hampton Cokeley", "Phoebe Lin"]
    deep = ["Danny Pohlman", "Michael Rooney"]

    total = 0.0
    with httpx.Client() as http:
        print("################ SONAR PRO ################")
        for nm in pro:
            row = _person(conn, nm)
            if not row:
                print(f"\n(skip {nm}: not in roster)"); continue
            res = query_sonar(http, key, "sonar-pro", row["full_name"], row["company"], row["city"])
            total += _report("sonar-pro", nm, res)
        print("\n################ SONAR DEEP RESEARCH (hard empties) ################")
        for nm in deep:
            row = _person(conn, nm)
            if not row:
                continue
            res = query_sonar(http, key, "sonar-deep-research", row["full_name"], row["company"], row["city"])
            total += _report("deep-research", nm, res)

    conn.close()
    print(f"\n================ TOTAL Sonar spend: ${total:.4f} ================")


if __name__ == "__main__":
    main()
