"""Unit tests for the pure parsing helpers in perplexity_agent.

The network call (`run_agent`) is exercised by agent_bakeoff against the live API;
here we lock down the response-parsing logic that turns a raw /v1/agent body into
typed mentions + cost, including the lenient JSON recovery and tool-call counting.
"""
from __future__ import annotations

from perplexity_agent import (
    AgentResult,
    _extract_mentions,
    _loads_lenient,
    _tool_calls,
    build_prompt,
)


def _message_body(text: str) -> dict:
    return {"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}


def test_extract_mentions_clean_json():
    body = _message_body(
        '{"mentions": ['
        '{"title": "Bio", "url": "https://x.com/a", "snippet": "s", '
        '"is_this_person": true, "reason": "match"},'
        '{"title": "Namesake", "url": "https://y.com/b", "snippet": "", '
        '"is_this_person": false, "reason": "different person"}]}'
    )
    rows = _extract_mentions(body)
    assert len(rows) == 2
    assert rows[0].title == "Bio" and rows[0].is_this_person is True
    assert rows[1].is_this_person is False


def test_extract_mentions_drops_rows_missing_title_or_url():
    body = _message_body(
        '{"mentions": ['
        '{"title": "", "url": "https://x.com", "snippet": "", "is_this_person": true, "reason": ""},'
        '{"title": "Ok", "url": "", "snippet": "", "is_this_person": true, "reason": ""},'
        '{"title": "Keep", "url": "https://z.com", "snippet": "", "is_this_person": true, "reason": ""}]}'
    )
    rows = _extract_mentions(body)
    assert [r.title for r in rows] == ["Keep"]


def test_extract_mentions_handles_fenced_json():
    body = _message_body(
        'Here you go:\n```json\n{"mentions": [{"title": "T", "url": "https://q.com", '
        '"snippet": "s", "is_this_person": true, "reason": "r"}]}\n```'
    )
    rows = _extract_mentions(body)
    assert len(rows) == 1 and rows[0].url == "https://q.com"


def test_extract_mentions_empty_when_no_message():
    assert _extract_mentions({"output": [{"type": "search_results"}]}) == []
    assert _extract_mentions({}) == []


def test_loads_lenient_recovers_embedded_object():
    assert _loads_lenient('prefix {"a": 1} suffix') == {"a": 1}
    assert _loads_lenient("not json at all") is None


def test_tool_calls_counts_invocations():
    usage = {"tool_calls_details": {"search_web": {"invocation": 2}, "search_people": {"invocation": 1}}}
    assert _tool_calls(usage) == {"search_web": 2, "search_people": 1}


def test_tool_calls_handles_plain_int_and_missing():
    assert _tool_calls({"tool_calls_details": {"x": 3}}) == {"x": 3}
    assert _tool_calls({}) == {}


def test_confirmed_filters_to_matches():
    from perplexity_agent import AgentMention

    res = AgentResult(
        mentions=(
            AgentMention("a", "u1", "", True, ""),
            AgentMention("b", "u2", "", False, ""),
        ),
        cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
    )
    assert len(res.confirmed) == 1 and res.confirmed[0].title == "a"


def test_build_prompt_includes_employer_and_city_when_meaningful():
    p = build_prompt("Jane Doe", "Acme Capital", "Austin")
    assert "Jane Doe" in p and "Acme Capital" in p and "Austin" in p


def test_build_prompt_omits_generic_employer():
    # has_meaningful_employer rejects all-generic strings (every token in the
    # generic set), so "Capital Group" adds no disambiguating signal.
    p = build_prompt("Jane Doe", "Capital Group", "")
    assert "Jane Doe" in p
    assert "Known employer" not in p
