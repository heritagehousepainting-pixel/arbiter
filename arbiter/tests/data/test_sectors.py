"""Tests for the ticker -> GICS sector map (WP W-SECTOR).

Backs the "20% per-sector" cap in policy/decision.py
(docs/audit/A2-risk-caps-gate.md). Pure, no network.
"""
from __future__ import annotations

import pytest

from arbiter.data.sectors import (
    GICS_SECTORS,
    UNKNOWN,
    covered_tickers,
    sector_for,
    sector_map,
)
from arbiter.data.sectors import _SECTOR_BY_TICKER
from arbiter.ingest.runner import _DEFAULT_WATCHLIST


# --- known tickers map to expected sectors ---------------------------------

@pytest.mark.parametrize(
    "ticker,expected",
    [
        ("AAPL", "Information Technology"),
        ("MSFT", "Information Technology"),
        ("NVDA", "Information Technology"),
        ("AMD", "Information Technology"),
        ("FN", "Information Technology"),
        ("GOOGL", "Communication Services"),
        ("META", "Communication Services"),
        ("T", "Communication Services"),
        ("AMZN", "Consumer Discretionary"),
        ("TSLA", "Consumer Discretionary"),
        ("UBER", "Consumer Discretionary"),
        ("ETN", "Industrials"),
        ("JPM", "Financials"),
        ("BRK.B", "Financials"),
        ("UNH", "Health Care"),
        ("XOM", "Energy"),
        # --- GICS traps + newly added round-out rows (P2-1) ---
        ("PYPL", "Financials"),               # PayPal is Financials, not IT
        ("AMT", "Real Estate"),               # American Tower is a REIT, not Utilities/IT
        ("LIN", "Materials"),                 # Linde (industrial gases)
        ("NEE", "Utilities"),                 # NextEra Energy
        ("CMCSA", "Communication Services"),  # Comcast
        ("LOW", "Consumer Discretionary"),    # Lowe's
        ("ABT", "Health Care"),               # Abbott
        ("SLB", "Energy"),                    # Schlumberger
        ("MO", "Consumer Staples"),           # Altria
        ("UNP", "Industrials"),               # Union Pacific
        ("ACN", "Information Technology"),     # Accenture
    ],
)
def test_known_tickers_map_to_expected_sector(ticker, expected):
    assert sector_for(ticker) == expected


def test_every_mapped_sector_is_a_valid_gics_label():
    # Each known ticker resolves to a recognised GICS top-level sector.
    for ticker in ("AAPL", "T", "ETN", "JPM", "UNH", "XOM", "UBER"):
        assert sector_for(ticker) in GICS_SECTORS


def test_every_table_value_is_valid_gics():
    # Core UNKNOWN-split guard: a mistyped/phantom sector label on ANY row
    # would split the per-sector cap into a ghost 12th bucket. Catch it here.
    for ticker, sector in _SECTOR_BY_TICKER.items():
        assert sector in GICS_SECTORS, (ticker, sector)


def test_no_duplicate_or_empty_keys():
    # sector_for upper-cases + strips its input, so every key must already be
    # a non-empty, upper-cased, stripped symbol or the row is unreachable.
    for key in _SECTOR_BY_TICKER:
        assert key, "empty ticker key"
        assert key == key.strip().upper(), key


def test_three_thin_buckets_now_populated():
    # These GICS buckets were previously EMPTY -> any such name collapsed to
    # UNKNOWN and merged into the catch-all sector cap. Prove they now exist.
    assert sector_for("LIN") == "Materials"
    assert sector_for("PLD") == "Real Estate"
    assert sector_for("NEE") == "Utilities"
    populated = set(_SECTOR_BY_TICKER.values())
    for bucket in ("Materials", "Real Estate", "Utilities"):
        assert bucket in populated


_ADDED_TICKERS = (
    # IT
    "ACN", "IBM", "NOW", "INTU", "LRCX", "KLAC", "ADI", "PANW", "SNPS", "CDNS",
    # Communication Services
    "CMCSA", "CHTR",
    # Consumer Discretionary
    "LOW", "BKNG", "TJX", "LULU", "GM", "F", "ABNB",
    # Industrials
    "UNP", "LMT", "NOC", "GD", "MMM", "EMR", "CSX", "FDX",
    # Financials
    "C", "SCHW", "BLK", "SPGI", "CB", "PGR", "PYPL",
    # Health Care
    "ABT", "DHR", "BMY", "AMGN", "GILD", "CVS", "ISRG", "MDT", "VRTX",
    # Energy
    "SLB", "EOG", "MPC", "PSX", "WMB", "OXY",
    # Consumer Staples
    "MO", "PM", "MDLZ", "CL", "TGT", "KMB",
    # Materials
    "LIN", "SHW", "APD", "FCX", "NEM", "ECL", "DOW",
    # Real Estate
    "PLD", "AMT", "EQIX", "SPG", "O", "CCI",
    # Utilities
    "NEE", "DUK", "SO", "D", "AEP", "EXC",
)


@pytest.mark.parametrize("ticker", _ADDED_TICKERS)
def test_all_added_tickers_resolve_non_unknown(ticker):
    # Guards against any added row being lost in a future merge.
    assert sector_for(ticker) != UNKNOWN


# --- the default ingest watchlist is fully covered (P2-2 invariant) --------

def test_default_watchlist_fully_mapped():
    # Import the REAL watchlist from runner.py so editing it there cannot
    # silently leave a name uncovered (the machine-checked sync invariant).
    mapped = sector_map(_DEFAULT_WATCHLIST)
    unknown = [t for t, s in mapped.items() if s == UNKNOWN]
    assert not unknown, f"watchlist tickers missing from sector map: {unknown}"


def test_watchlist_is_subset_of_covered_tickers():
    # Superset relation via the public accessor (no private-name reach-in).
    covered = covered_tickers()
    missing = [t for t in _DEFAULT_WATCHLIST if t.strip().upper() not in covered]
    assert not missing, missing


# --- unknown / empty -> UNKNOWN --------------------------------------------

@pytest.mark.parametrize("ticker", ["ZZZZ", "NOTREAL", "", "   "])
def test_unknown_ticker_is_unknown(ticker):
    assert sector_for(ticker) == UNKNOWN


# --- case / whitespace insensitivity ---------------------------------------

def test_lookup_is_case_and_whitespace_insensitive():
    assert sector_for("aapl") == "Information Technology"
    assert sector_for("  Uber ") == "Consumer Discretionary"


# --- sector_map round-trips -------------------------------------------------

def test_sector_map_round_trips():
    tickers = ["AAPL", "T", "ETN", "ZZZZ"]
    result = sector_map(tickers)
    assert set(result.keys()) == set(tickers)
    assert result == {
        "AAPL": "Information Technology",
        "T": "Communication Services",
        "ETN": "Industrials",
        "ZZZZ": UNKNOWN,
    }
    # Keys preserved exactly as supplied; values agree with sector_for.
    for t in tickers:
        assert result[t] == sector_for(t)


def test_sector_map_empty_input():
    assert sector_map([]) == {}


def test_sector_map_preserves_input_key_casing():
    # Keys are the input symbols verbatim, even when lookup is case-insensitive.
    assert sector_map(["aapl"]) == {"aapl": "Information Technology"}
