"""EDGAR Form 4 ingestion adapter — Lane 5a.

Public surface
--------------
- ``EdgarClient``  : fetches raw XML from EDGAR (rate-limit aware; mocked in tests).
                     Discovery uses the submissions-JSON transport for both
                     Form-4 (``search_form4_filings`` / ``get_form4_xml``) and
                     13D/13G (``search_sc13_filings`` / ``get_sc13_doc``).
                     ``from_config_or_none`` returns ``None`` (one WARNING)
                     when ``EDGAR_USER_AGENT`` is unset — the whole lane goes
                     inert without crashing.
- ``parse_form4``  : Form 4 XML → list[dict]
- ``normalize``    : thin wrapper that applies all Form-4 business rules and
                     returns only the filings that should reach the writer agent
- ``parse_sc13``   : Schedule 13D/13G document → list[dict]
- ``normalize_sc13``: 13D/13G business rules → list[RawFiling] (source="form13d")

RawFiling schema (TypedDict defined in this package):

    {
        source:        "form4",
        ticker:        str,
        person_id:     str,       # CIK of the reporting owner
        person_name:   str,
        filing_ts:     str,       # tz-aware ISO-8601 (UTC)
        txn_type:      "P" | "S",
        shares:        float,
        price:         float,
        amount_low:    float,
        amount_high:   float,
        is_10b5_1:     bool,
        is_amendment:  bool,
        accession:     str,
        raw_json:      str,       # json.dumps of the full parsed dict
    }

Supersede marker (emitted when an amendment direction changes)
---------------------------------------------------------------
When ``is_amendment`` is True and the caller detects a direction flip versus an
existing filing, they should call the writer agent with a ``_supersedes``
key added to the dict (writer calls ``supersede_row``).  This adapter sets the
``is_amendment`` flag; the writer handles the DB flip.
"""
from __future__ import annotations

from arbiter.ingest.edgar.client import EdgarClient
from arbiter.ingest.edgar.parser import parse_form4
from arbiter.ingest.edgar.normalize import normalize, RawFiling
from arbiter.ingest.edgar.sc13_parser import parse_sc13
from arbiter.ingest.edgar.sc13_normalize import normalize_sc13

__all__ = [
    "EdgarClient",
    "parse_form4",
    "normalize",
    "parse_sc13",
    "normalize_sc13",
    "RawFiling",
]
