"""Unit tests for the unified research-policy gate criteria."""
from __future__ import annotations

import pytest

from research_policy import (
    ResearchPolicy,
    bypass_linkedin_gap_gate,
    force_deep_path,
)


def test_parse_valid_values():
    assert ResearchPolicy.parse("bulk") is ResearchPolicy.BULK
    assert ResearchPolicy.parse("DEEP") is ResearchPolicy.DEEP
    assert ResearchPolicy.parse("  Refresh ") is ResearchPolicy.REFRESH


def test_parse_invalid_value_names_choices():
    with pytest.raises(ValueError, match="bulk, deep, refresh"):
        ResearchPolicy.parse("turbo")


def test_bulk_enforces_every_gate():
    assert not force_deep_path(ResearchPolicy.BULK)
    assert not bypass_linkedin_gap_gate(ResearchPolicy.BULK)


def test_deep_forces_deep_path_but_keeps_linkedin_gap_gate():
    assert force_deep_path(ResearchPolicy.DEEP)
    assert not bypass_linkedin_gap_gate(ResearchPolicy.DEEP)


def test_refresh_opens_both_criteria():
    assert force_deep_path(ResearchPolicy.REFRESH)
    assert bypass_linkedin_gap_gate(ResearchPolicy.REFRESH)


def test_policy_is_string_enum_for_cli_round_trip():
    # argparse/json round-trips rely on the value being the plain string.
    assert ResearchPolicy.REFRESH.value == "refresh"
    assert ResearchPolicy(ResearchPolicy.DEEP.value) is ResearchPolicy.DEEP
