"""Cross-language guard: the web profile-link drop list must stay in sync with the
pipeline's broker/records host classification.

If a data-broker or public-records host is added to the pipeline (directory_hosts.py)
but NOT to web/lib/link-quality.ts, that host would keep showing as a profile
"appearance" on the web. As the discovery funnel widens (more surfaced documents),
this drift is exactly the failure we must prevent. This test fails loudly on drift.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from directory_hosts import DIRECTORY_HOSTS, PUBLIC_RECORDS_HOSTS, SOCIAL_HOSTS

_LINK_QUALITY_TS = (
    Path(__file__).resolve().parents[2] / "web" / "lib" / "link-quality.ts"
)

# Hosts allowed to exist ONLY on the web side (no pipeline equivalent needed):
# subdomain duplicates of a covered registrable domain, or web-only noise hosts.
_TS_ONLY_ALLOWED = frozenset({"app.getwarmer.com", "loopnet.com"})


def _ts_drop_hosts() -> set[str]:
    text = _LINK_QUALITY_TS.read_text(encoding="utf-8")
    m = re.search(r"const DROP_HOSTS = new Set<string>\(\[(.*?)\]\)", text, re.DOTALL)
    assert m, "could not locate DROP_HOSTS in link-quality.ts"
    return set(re.findall(r'"([^"]+)"', m.group(1)))


def _registrable(host: str) -> str:
    parts = host.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


@pytest.mark.unit
def test_pipeline_broker_hosts_are_dropped_on_web() -> None:
    """Every directory/data-broker host the pipeline distrusts must also be dropped
    from the web profile links."""
    drop = _ts_drop_hosts()
    missing = sorted(h for h in DIRECTORY_HOSTS if h not in drop)
    assert not missing, f"broker hosts in directory_hosts.py missing from link-quality.ts: {missing}"


@pytest.mark.unit
def test_pipeline_records_hosts_are_dropped_on_web() -> None:
    drop = _ts_drop_hosts()
    missing = sorted(h for h in PUBLIC_RECORDS_HOSTS if h not in drop)
    assert not missing, f"records hosts in directory_hosts.py missing from link-quality.ts: {missing}"


@pytest.mark.unit
def test_no_unexplained_web_only_drop_hosts() -> None:
    """A host added to link-quality.ts should trace back to a pipeline set (or the
    explicit allowlist) — catches the reverse drift. LinkedIn is handled by a
    dedicated check in usefulLinks, so it's expected to be absent here."""
    drop = _ts_drop_hosts()
    known = DIRECTORY_HOSTS | PUBLIC_RECORDS_HOSTS | SOCIAL_HOSTS
    orphan = sorted(
        h for h in drop
        if h not in _TS_ONLY_ALLOWED
        and h not in known
        and _registrable(h) not in known
    )
    assert not orphan, f"link-quality.ts hosts with no pipeline equivalent: {orphan}"
