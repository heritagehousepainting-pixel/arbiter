"""Vendored static sector + valuation baseline (COARSE HEURISTIC).

This module is a deliberately small, *static* approximation, not a live market
cross-section. A2 analyzes one ticker per call and has no bulk-ratio egress
(its only outbound hosts are SEC + Alpaca), so a true peer-P/E cross-section is
not reachable. Instead we vendor:

  - SECTOR_MAP:        ticker (upper) -> coarse sector label
  - SECTOR_PE_BASELINE: sector       -> (median_pe, stdev_pe)

`valuation_z = (pe_ratio - median_pe) / stdev_pe` (computed in fundamentals.py)
gives a rough "richer / cheaper than my sector" signal. The numbers are
hand-set order-of-magnitude baselines reflecting long-run sector P/E norms;
they are NOT precise and should be treated as a directional prior only.

ISOLATION: pure stdlib data. Never imports arbiter.
"""
from __future__ import annotations

# Sector -> (median trailing P/E, stdev of P/E). Coarse, static, directional.
SECTOR_PE_BASELINE: dict[str, tuple[float, float]] = {
    "Technology": (28.0, 12.0),
    "Communication Services": (22.0, 9.0),
    "Consumer Discretionary": (24.0, 11.0),
    "Consumer Staples": (21.0, 6.0),
    "Health Care": (23.0, 9.0),
    "Financials": (13.0, 5.0),
    "Industrials": (20.0, 7.0),
    "Energy": (12.0, 6.0),
    "Materials": (16.0, 6.0),
    "Utilities": (19.0, 5.0),
    "Real Estate": (30.0, 12.0),
}

# Ticker (UPPER) -> coarse sector. Covers the arbiter watchlist + common names.
SECTOR_MAP: dict[str, str] = {
    # --- Technology ---
    "AAPL": "Technology",
    "MSFT": "Technology",
    "NVDA": "Technology",
    "AVGO": "Technology",
    "ORCL": "Technology",
    "CRM": "Technology",
    "ADBE": "Technology",
    "AMD": "Technology",
    "INTC": "Technology",
    "CSCO": "Technology",
    "QCOM": "Technology",
    "TXN": "Technology",
    "IBM": "Technology",
    "MU": "Technology",
    "AMAT": "Technology",
    "NOW": "Technology",
    "PANW": "Technology",
    "SNOW": "Technology",
    "PLTR": "Technology",
    "SMCI": "Technology",
    "ARM": "Technology",
    "DELL": "Technology",
    "HPQ": "Technology",
    # --- Communication Services ---
    "GOOGL": "Communication Services",
    "GOOG": "Communication Services",
    "META": "Communication Services",
    "NFLX": "Communication Services",
    "DIS": "Communication Services",
    "CMCSA": "Communication Services",
    "T": "Communication Services",
    "VZ": "Communication Services",
    "TMUS": "Communication Services",
    # --- Consumer Discretionary ---
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",
    "LOW": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary",
    "BKNG": "Consumer Discretionary",
    "TGT": "Consumer Discretionary",
    "F": "Consumer Discretionary",
    "GM": "Consumer Discretionary",
    "ABNB": "Consumer Discretionary",
    # --- Consumer Staples ---
    "WMT": "Consumer Staples",
    "COST": "Consumer Staples",
    "PG": "Consumer Staples",
    "KO": "Consumer Staples",
    "PEP": "Consumer Staples",
    "PM": "Consumer Staples",
    "MO": "Consumer Staples",
    "MDLZ": "Consumer Staples",
    "CL": "Consumer Staples",
    # --- Health Care ---
    "UNH": "Health Care",
    "JNJ": "Health Care",
    "LLY": "Health Care",
    "MRK": "Health Care",
    "ABBV": "Health Care",
    "PFE": "Health Care",
    "TMO": "Health Care",
    "ABT": "Health Care",
    "DHR": "Health Care",
    "BMY": "Health Care",
    "AMGN": "Health Care",
    "GILD": "Health Care",
    "CVS": "Health Care",
    # --- Financials ---
    "BRK.B": "Financials",
    "JPM": "Financials",
    "V": "Financials",
    "MA": "Financials",
    "BAC": "Financials",
    "WFC": "Financials",
    "GS": "Financials",
    "MS": "Financials",
    "C": "Financials",
    "AXP": "Financials",
    "SCHW": "Financials",
    "BLK": "Financials",
    "SPGI": "Financials",
    "PYPL": "Financials",
    # --- Industrials ---
    "CAT": "Industrials",
    "BA": "Industrials",
    "HON": "Industrials",
    "GE": "Industrials",
    "UPS": "Industrials",
    "RTX": "Industrials",
    "LMT": "Industrials",
    "DE": "Industrials",
    "UNP": "Industrials",
    "MMM": "Industrials",
    "FDX": "Industrials",
    # --- Energy ---
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "SLB": "Energy",
    "EOG": "Energy",
    "MPC": "Energy",
    "PSX": "Energy",
    "OXY": "Energy",
    # --- Materials ---
    "LIN": "Materials",
    "SHW": "Materials",
    "APD": "Materials",
    "FCX": "Materials",
    "NEM": "Materials",
    "DOW": "Materials",
    # --- Utilities ---
    "NEE": "Utilities",
    "DUK": "Utilities",
    "SO": "Utilities",
    "D": "Utilities",
    "AEP": "Utilities",
    # --- Real Estate ---
    "PLD": "Real Estate",
    "AMT": "Real Estate",
    "EQIX": "Real Estate",
    "SPG": "Real Estate",
    "O": "Real Estate",
}


def sector_for(ticker: str) -> str | None:
    """Return the coarse vendored sector for a ticker, or None if unmapped."""
    return SECTOR_MAP.get(ticker.upper())


def pe_baseline_for(sector: str | None) -> tuple[float, float] | None:
    """Return (median_pe, stdev_pe) for a sector, or None if unmapped."""
    if sector is None:
        return None
    return SECTOR_PE_BASELINE.get(sector)
