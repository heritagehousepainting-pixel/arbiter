"""Static ticker -> GICS sector mapping (WP W-SECTOR).

Today ``policy/decision.py`` defaults every ticker to ``"UNKNOWN"``, which
collapses the "20% per-sector" cap into a single book-wide bucket (see
``docs/audit/A2-risk-caps-gate.md`` and ``docs/audit/L1-roadmap-honesty.md``).
This module supplies the real mapping the engine will later thread into
``decide(sector_by_ticker=...)``.

API::

    sector_for(ticker)      -> str             # e.g. "Information Technology"
    sector_map(tickers)     -> dict[str, str]  # {ticker: sector} for each input

Sectors follow the GICS top-level taxonomy (Information Technology,
Communication Services, Industrials, Consumer Discretionary, Financials,
Health Care, Energy, Consumer Staples, Materials, Real Estate, Utilities).

Design notes:
    * Pure and deterministic — no I/O, no network, no ``datetime.now()``.
    * Lookup is case-insensitive on the ticker symbol.
    * Unmapped tickers default to ``"UNKNOWN"`` (fail-soft; the caller treats
      an unknown sector as its own bucket rather than crashing).

How to extend:
    Add the ``"SYMBOL": "GICS Sector"`` pair to ``_SECTOR_BY_TICKER`` below,
    keying on the upper-cased symbol and using one of the ``GICS_SECTORS``
    strings as the value. Keep this table in sync with the ingest watchlist
    (``arbiter/ingest/runner.py::_DEFAULT_WATCHLIST``) plus any symbols that
    have actually traded.

Eventual upgrade:
    This hand-maintained table is a stopgap. The real fix is to source sector
    classifications from a GICS/SIC data provider (e.g. the SEC company facts
    SIC code, or a vendor feed) so the universe stays correct without manual
    edits. When that lands, replace ``_SECTOR_BY_TICKER`` with a provider-backed
    lookup while keeping this module's ``sector_for`` / ``sector_map`` API.
"""
from __future__ import annotations

from typing import Iterable

__all__ = ["sector_for", "sector_map", "covered_tickers", "GICS_SECTORS", "UNKNOWN"]

UNKNOWN = "UNKNOWN"

#: The GICS top-level sector strings used as mapping values.
GICS_SECTORS: frozenset[str] = frozenset(
    {
        "Information Technology",
        "Communication Services",
        "Industrials",
        "Consumer Discretionary",
        "Financials",
        "Health Care",
        "Energy",
        "Consumer Staples",
        "Materials",
        "Real Estate",
        "Utilities",
    }
)

# Static ticker -> GICS sector table. Keys are UPPER-CASED symbols.
# Covers the ingest default watchlist plus symbols that have actually traded.
_SECTOR_BY_TICKER: dict[str, str] = {
    # --- Information Technology ---
    "AAPL": "Information Technology",
    "MSFT": "Information Technology",
    "NVDA": "Information Technology",
    "AMD": "Information Technology",
    "FN": "Information Technology",  # Fabrinet — electronic mfg services
    "AVGO": "Information Technology",
    "ORCL": "Information Technology",
    "CRM": "Information Technology",
    "ADBE": "Information Technology",
    "INTC": "Information Technology",
    "QCOM": "Information Technology",
    "TXN": "Information Technology",
    "MU": "Information Technology",
    "AMAT": "Information Technology",
    "CSCO": "Information Technology",
    "ACN": "Information Technology",  # Accenture
    "IBM": "Information Technology",  # IBM
    "NOW": "Information Technology",  # ServiceNow
    "INTU": "Information Technology",  # Intuit
    "LRCX": "Information Technology",  # Lam Research
    "KLAC": "Information Technology",  # KLA Corp
    "ADI": "Information Technology",  # Analog Devices
    "PANW": "Information Technology",  # Palo Alto Networks
    "SNPS": "Information Technology",  # Synopsys
    "CDNS": "Information Technology",  # Cadence Design Systems
    # --- Communication Services ---
    "GOOGL": "Communication Services",
    "GOOG": "Communication Services",
    "META": "Communication Services",
    "T": "Communication Services",  # AT&T
    "VZ": "Communication Services",
    "NFLX": "Communication Services",
    "DIS": "Communication Services",
    "TMUS": "Communication Services",
    "CMCSA": "Communication Services",  # Comcast
    "CHTR": "Communication Services",  # Charter Communications
    # --- Consumer Discretionary ---
    "AMZN": "Consumer Discretionary",
    "TSLA": "Consumer Discretionary",
    "UBER": "Consumer Discretionary",
    "HD": "Consumer Discretionary",
    "MCD": "Consumer Discretionary",
    "NKE": "Consumer Discretionary",
    "SBUX": "Consumer Discretionary",
    "LOW": "Consumer Discretionary",  # Lowe's
    "BKNG": "Consumer Discretionary",  # Booking Holdings
    "TJX": "Consumer Discretionary",  # TJX Companies
    "LULU": "Consumer Discretionary",  # Lululemon
    "GM": "Consumer Discretionary",  # General Motors
    "F": "Consumer Discretionary",  # Ford
    "ABNB": "Consumer Discretionary",  # Airbnb
    # --- Industrials ---
    "ETN": "Industrials",  # Eaton Corp
    "CAT": "Industrials",
    "BA": "Industrials",
    "GE": "Industrials",
    "HON": "Industrials",
    "UPS": "Industrials",
    "DE": "Industrials",
    "RTX": "Industrials",
    "UNP": "Industrials",  # Union Pacific
    "LMT": "Industrials",  # Lockheed Martin
    "NOC": "Industrials",  # Northrop Grumman
    "GD": "Industrials",  # General Dynamics
    "MMM": "Industrials",  # 3M
    "EMR": "Industrials",  # Emerson Electric
    "CSX": "Industrials",  # CSX Corp
    "FDX": "Industrials",  # FedEx
    # --- Financials ---
    "JPM": "Financials",
    "BRK.B": "Financials",
    "BAC": "Financials",
    "WFC": "Financials",
    "GS": "Financials",
    "MS": "Financials",
    "V": "Financials",
    "MA": "Financials",
    "AXP": "Financials",
    "C": "Financials",  # Citigroup
    "SCHW": "Financials",  # Charles Schwab
    "BLK": "Financials",  # BlackRock
    "SPGI": "Financials",  # S&P Global
    "CB": "Financials",  # Chubb
    "PGR": "Financials",  # Progressive
    "PYPL": "Financials",  # PayPal (GICS: Financials)
    # --- Health Care ---
    "UNH": "Health Care",
    "JNJ": "Health Care",
    "LLY": "Health Care",
    "PFE": "Health Care",
    "ABBV": "Health Care",
    "MRK": "Health Care",
    "TMO": "Health Care",
    "ABT": "Health Care",  # Abbott Laboratories
    "DHR": "Health Care",  # Danaher
    "BMY": "Health Care",  # Bristol-Myers Squibb
    "AMGN": "Health Care",  # Amgen
    "GILD": "Health Care",  # Gilead Sciences
    "CVS": "Health Care",  # CVS Health
    "ISRG": "Health Care",  # Intuitive Surgical
    "MDT": "Health Care",  # Medtronic
    "VRTX": "Health Care",  # Vertex Pharmaceuticals
    # --- Energy ---
    "XOM": "Energy",
    "CVX": "Energy",
    "COP": "Energy",
    "SLB": "Energy",  # Schlumberger
    "EOG": "Energy",  # EOG Resources
    "MPC": "Energy",  # Marathon Petroleum
    "PSX": "Energy",  # Phillips 66
    "WMB": "Energy",  # Williams Companies
    "OXY": "Energy",  # Occidental Petroleum
    # --- Consumer Staples ---
    "PG": "Consumer Staples",
    "KO": "Consumer Staples",
    "PEP": "Consumer Staples",
    "WMT": "Consumer Staples",
    "COST": "Consumer Staples",
    "MO": "Consumer Staples",  # Altria
    "PM": "Consumer Staples",  # Philip Morris International
    "MDLZ": "Consumer Staples",  # Mondelez International
    "CL": "Consumer Staples",  # Colgate-Palmolive
    "TGT": "Consumer Staples",  # Target
    "KMB": "Consumer Staples",  # Kimberly-Clark
    # --- Materials (previously EMPTY -> collapsed to UNKNOWN) ---
    "LIN": "Materials",  # Linde
    "SHW": "Materials",  # Sherwin-Williams
    "APD": "Materials",  # Air Products & Chemicals
    "FCX": "Materials",  # Freeport-McMoRan
    "NEM": "Materials",  # Newmont
    "ECL": "Materials",  # Ecolab
    "DOW": "Materials",  # Dow Inc
    # --- Real Estate (previously EMPTY -> collapsed to UNKNOWN) ---
    "PLD": "Real Estate",  # Prologis
    "AMT": "Real Estate",  # American Tower
    "EQIX": "Real Estate",  # Equinix
    "SPG": "Real Estate",  # Simon Property Group
    "O": "Real Estate",  # Realty Income
    "CCI": "Real Estate",  # Crown Castle
    # --- Utilities (previously EMPTY -> collapsed to UNKNOWN) ---
    "NEE": "Utilities",  # NextEra Energy
    "DUK": "Utilities",  # Duke Energy
    "SO": "Utilities",  # Southern Company
    "D": "Utilities",  # Dominion Energy
    "AEP": "Utilities",  # American Electric Power
    "EXC": "Utilities",  # Exelon
}


def sector_for(ticker: str) -> str:
    """Return the GICS sector for ``ticker``, or ``"UNKNOWN"`` if unmapped.

    The lookup is case-insensitive and tolerant of surrounding whitespace.
    Empty / falsy input returns ``"UNKNOWN"``.

    Parameters
    ----------
    ticker:
        Equity symbol, e.g. ``"AAPL"`` or ``"aapl"``.

    Returns
    -------
    str
        One of the :data:`GICS_SECTORS` strings, or :data:`UNKNOWN`.
    """
    if not ticker:
        return UNKNOWN
    return _SECTOR_BY_TICKER.get(ticker.strip().upper(), UNKNOWN)


def sector_map(tickers: Iterable[str]) -> dict[str, str]:
    """Return ``{ticker: sector}`` for each ticker in ``tickers``.

    Keys are the input symbols exactly as supplied (so callers can join the
    result back against their own ticker list). Unmapped symbols map to
    :data:`UNKNOWN`.

    Suitable for passing as ``decide(sector_by_ticker=sector_map(universe))``.
    """
    return {ticker: sector_for(ticker) for ticker in tickers}


def covered_tickers() -> frozenset[str]:
    """Return the set of UPPER-CASED symbols with an explicit sector mapping.

    Anything outside this set resolves to :data:`UNKNOWN`. Useful for asserting
    coverage invariants (e.g. that the ingest watchlist is a subset) without
    reaching into the private ``_SECTOR_BY_TICKER`` table.
    """
    return frozenset(_SECTOR_BY_TICKER)
