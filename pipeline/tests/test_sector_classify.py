"""Unit tests for the python sector classifier (mirror of web classifySector)."""
from __future__ import annotations

from sector_classify import SECTOR_CATCHALL, classify_sector


def test_investment_banking():
    assert classify_sector("Goldman Sachs") == "Investment Banking"
    assert classify_sector("J.P. Morgan") == "Investment Banking"


def test_consulting_and_accounting():
    assert classify_sector("McKinsey & Company") == "Consulting"
    assert classify_sector("Deloitte Consulting") == "Consulting"  # 'consulting' & 'deloitte' — IB? no; consulting rule first after IB
    assert classify_sector("KPMG") == "Accounting & Audit"


def test_private_equity_and_hedge():
    assert classify_sector("Blackstone") == "Private Equity & Credit"
    assert classify_sector("Citadel") == "Hedge Funds & Asset Mgmt"
    assert classify_sector("Acme Asset Management") == "Hedge Funds & Asset Mgmt"


def test_energy():
    assert classify_sector("EnCap Investments") == "Energy & Real Assets"
    assert classify_sector("Chevron") == "Energy & Real Assets"


def test_catchall_and_empty():
    assert classify_sector("Some Local Bakery") == SECTOR_CATCHALL
    assert classify_sector("") == SECTOR_CATCHALL
    assert classify_sector("   ") == SECTOR_CATCHALL


def test_first_rule_wins_ordering():
    # "Bain Capital" contains both 'bain' (Consulting) and 'bain capital' (PE);
    # Consulting is tested first, so it wins — matches the web's first-match rule.
    assert classify_sector("Bain Capital") == "Consulting"
