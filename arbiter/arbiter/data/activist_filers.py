"""Static roster of tracked activist 13D filers (the A1.activist universe).

The 13D channel has two discovery paths:

* **Subject-search** (``runner._ingest_sc13`) — "who filed a 13D *against* one
  of our watchlist tickers?"  Good for activists targeting names we already
  watch, blind to everything else.
* **Filer-search** (``runner._ingest_sc13_by_filer``) — "what has THIS known
  activist filed recently, against *anyone*?"  This module supplies that roster.

Each entry maps a famous activist to the EDGAR **filer CIK** under which they
actually submit Schedule 13D filings today (NOT a 13F management-company CIK and
NOT the subject company).  Mirrors ``data.fund_managers`` exactly; extending the
roster is one verified line.

Every CIK below was verified live against EDGAR on 2026-06-24 via
``data.sec.gov/submissions/CIK<cik>.json`` — the recorded ``name`` and recent
13D count / latest 13D date are inlined so drift is auditable.
"""
from __future__ import annotations

from typing import NamedTuple


class ActivistFiler(NamedTuple):
    name: str   # canonical person/firm name (for the people table / cockpit)
    fund: str   # filer entity name (as EDGAR reports it)
    cik: str    # 10-digit zero-padded EDGAR CIK of the 13D filer


# CIKs verified 2026-06-24 via data.sec.gov submissions JSON (SC 13D present).
# Trailing comment: (recent 13D count in submissions.recent | latest 13D date).
ACTIVIST_FILERS: tuple[ActivistFiler, ...] = (
    ActivistFiler("Carl Icahn", "ICAHN CARL C", "0000921669"),                       # 463 | 2026-06-09
    ActivistFiler("Jeff Smith", "Starboard Value LP", "0001517137"),                 # 457 | 2026-06-02
    ActivistFiler("Nelson Peltz", "Trian Fund Management, L.P.", "0001345471"),       # 144 | 2026-06-18
    ActivistFiler("Paul Singer", "Elliott Investment Management L.P.", "0001791786"), # 56  | 2026-04-03
    ActivistFiler("Barry Rosenstein", "JANA Partners Management, LP", "0001998597"),  # 28  | 2026-06-10
    ActivistFiler("Mason Morfit", "ValueAct Capital Master Fund, L.P.", "0001464912"),# 3   | 2025-05-06
)


def activist_ciks() -> tuple[str, ...]:
    """Return the tuple of all tracked activist filer CIKs (10-digit, padded)."""
    return tuple(a.cik for a in ACTIVIST_FILERS)
