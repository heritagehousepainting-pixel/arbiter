"""Static roster of tracked 13F fund managers (the A1.fund universe).

Each entry maps a famous manager to their 13F FILER cik (the management
company that files 13F-HR, NOT the natural person).  Extending the roster is
one line.  Every CIK below was verified live against EDGAR on 2026-06-23: the
submissions JSON ``name`` is recorded inline and each filer has ``13F-HR`` in
``filings.recent.form``.
"""
from __future__ import annotations

from typing import NamedTuple


class FundManager(NamedTuple):
    name: str   # canonical person name (for the people table / cockpit)
    fund: str   # filer entity name (as EDGAR reports it)
    cik: str    # 10-digit zero-padded EDGAR CIK of the 13F filer


# CIKs verified 2026-06-23 via data.sec.gov submissions JSON (13F-HR present).
FUND_MANAGERS: tuple[FundManager, ...] = (
    FundManager("Cathie Wood", "ARK Investment Management LLC", "0001697748"),
    FundManager("Michael Burry", "Scion Asset Management, LLC", "0001649339"),
    FundManager("Warren Buffett", "Berkshire Hathaway Inc", "0001067983"),
    FundManager("Bill Ackman", "Pershing Square Capital Management, L.P.", "0001336528"),
    FundManager("David Tepper", "Appaloosa LP", "0001656456"),
    FundManager("David Einhorn", "Greenlight Capital Inc", "0001079114"),
    FundManager("Stanley Druckenmiller", "Duquesne Family Office LLC", "0001536411"),
    FundManager("Seth Klarman", "Baupost Group LLC /ADV", "0001054420"),
    FundManager("Chase Coleman", "Tiger Global Management LLC", "0001167483"),
    FundManager("Daniel Loeb", "Third Point LLC", "0001040273"),
    FundManager("Leopold Aschenbrenner", "Situational Awareness LP", "0002045724"),
)


def manager_ciks() -> tuple[str, ...]:
    """Return the tuple of all tracked 13F filer CIKs (10-digit, zero-padded)."""
    return tuple(m.cik for m in FUND_MANAGERS)
