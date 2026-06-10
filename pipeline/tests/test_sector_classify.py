"""Unit tests for the python sector classifier (mirror of web classifySector)."""
from __future__ import annotations

from sector_classify import SECTOR_CATCHALL, SECTOR_NAMES, classify_sector


def test_investment_banking():
    assert classify_sector("Goldman Sachs") == "Investment Banking"
    assert classify_sector("J.P. Morgan") == "Investment Banking"


def test_consulting_and_accounting():
    assert classify_sector("McKinsey & Company") == "Consulting"
    assert classify_sector("Deloitte Consulting") == "Consulting"  # 'consulting' wins over 'deloitte'
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


def test_bain_capital_is_private_equity():
    # Narrowed Consulting to "bain & company"; "bain capital" is now PE, which is
    # correct (it's a PE firm, not the consultancy).
    assert classify_sector("Bain Capital") == "Private Equity & Credit"
    assert classify_sector("Bain & Company") == "Consulting"


# --- new sectors via employer-name keywords -------------------------------

def test_new_sectors_by_company_keyword():
    assert classify_sector("Kirkland & Ellis LLP") == "Law / Legal"
    assert classify_sector("CBRE Group") == "Real Estate"
    assert classify_sector("Abbott Laboratories") == "Healthcare & Life Sciences"
    assert classify_sector("Acme Insurance") == "Insurance"
    assert classify_sector("Stanford University") == "Education & Academia"
    assert classify_sector("Robin Hood Foundation") == "Government & Nonprofit"


# --- PDL industry is the authoritative primary signal ---------------------

def test_industry_overrides_unknown_company():
    # A firm name with no finance keyword would be catch-all, but the PDL
    # industry places it precisely.
    assert classify_sector("Vanguard Group Realty", "real estate") == "Real Estate"
    assert classify_sector("Smith & Associates", "law practice") == "Law / Legal"
    assert classify_sector("Acme Corp", "computer software") == "Technology"
    assert classify_sector("Methodist", "hospital & health care") == "Healthcare & Life Sciences"
    assert classify_sector("Anon LLC", "higher education") == "Education & Academia"
    assert classify_sector("Anon LLC", "non-profit organization management") == "Government & Nonprofit"


def test_industry_maps_finance_subsectors():
    assert classify_sector("Anon", "investment management") == "Hedge Funds & Asset Mgmt"
    assert classify_sector("Anon", "venture capital & private equity") == "Private Equity & Credit"
    assert classify_sector("Anon", "investment banking") == "Investment Banking"
    assert classify_sector("Anon", "management consulting") == "Consulting"


def test_generic_industry_falls_through_to_keyword():
    # Bare "financial services" is too generic to place — fall back to the firm
    # name, which here is a known IB.
    assert classify_sector("Goldman Sachs", "financial services") == "Investment Banking"
    # ...and to the catch-all when the name is unknown too.
    assert classify_sector("Acme Holdings Co", "financial services") in SECTOR_NAMES
    assert classify_sector("Joe's Diner", "financial services") == SECTOR_CATCHALL


def test_catchall_is_in_sector_names_last():
    assert SECTOR_NAMES[-1] == SECTOR_CATCHALL
    assert len(set(SECTOR_NAMES)) == len(SECTOR_NAMES)  # no duplicates
