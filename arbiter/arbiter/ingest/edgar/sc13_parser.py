"""Schedule 13D / 13G parser — produces intermediate dicts fed into
``normalize_sc13``.

A 13D/13G filing reports a >5% beneficial-ownership stake in a public company.
13D carries an *intent to influence/control* (activist); 13G is the passive
short-form variant.  We model 13D as the higher-conviction long signal.

On the wire these come in two shapes; both are supported, preferring the
structured one:

* **Structured XML** — a ``<edgarSubmission>`` / ``ownershipDocument``-style
  document carrying issuer/subject identifiers, the reporting (filing) person,
  ``<percentOfClass>``, ``<aggregateAmountOwned>``, the event date, and
  ``<documentType>`` (``SC 13D``, ``SC 13D/A``, ``SC 13G``, ``SC 13G/A``).
* **Header-only plain text** — an older ``.txt`` filing whose ``<SEC-HEADER>``
  block names ``SUBJECT COMPANY``, ``FILED BY``, ``CONFORMED SUBMISSION TYPE``,
  ``FILED AS OF DATE`` and ``CUSIP``.  Percent-of-class is regexed tolerantly
  from the body free-text (``"percent of class: 7.3%"``).

Output row dict (mirrors the Form-4 parser's contract so ``normalize_sc13`` is
trivial) — see ``parse_sc13`` for the exact keys.

Transaction code
----------------
``"P"`` for a new/increased stake (default); ``"S"`` for an exit/reduction.
A reduction is inferred when an amendment drops ``percent_of_class`` below the
5% reporting threshold (``< 5.0``).
"""
from __future__ import annotations

import re
from typing import Any
from xml.etree import ElementTree as ET

from arbiter.ingest.edgar.parser import (
    _float_or_none,
    _parse_filing_ts,
    _text,
)


# Tolerant "percent of class" regex for the header/plain-text fallback.
_PERCENT_OF_CLASS_RE = re.compile(
    r"percent\s+of\s+class[^0-9]{0,40}?([0-9]{1,3}(?:\.[0-9]+)?)\s*%",
    re.IGNORECASE | re.DOTALL,
)
# Generic percent fallback (last resort) — any "N%" token.
_ANY_PERCENT_RE = re.compile(r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%")

# SEC-HEADER field extractors (plain-text fallback).
_HDR_SUBMISSION_TYPE_RE = re.compile(
    r"CONFORMED\s+SUBMISSION\s+TYPE:\s*(\S+)", re.IGNORECASE
)
_HDR_FILED_AS_OF_RE = re.compile(
    r"FILED\s+AS\s+OF\s+DATE:\s*([0-9]{8})", re.IGNORECASE
)
# Tolerant of an intervening "No." / "Number" token before the value.
_HDR_CUSIP_RE = re.compile(
    r"CUSIP(?:\s*(?:No\.?|Number))?[^0-9A-Za-z]{0,6}([0-9]{6,8}[0-9A-Za-z]{0,3})",
    re.IGNORECASE,
)

# 5% SEC reporting threshold.
_REPORTING_THRESHOLD_PCT = 5.0


def _find_text(root: ET.Element, *tags: str) -> str:
    """Return the first non-empty ``.//<tag>`` text across ``tags``."""
    for tag in tags:
        val = _text(root.find(f".//{tag}"))
        if val:
            return val
    return ""


def _find_float_or_none(root: ET.Element, *tags: str) -> float | None:
    """Return the first parseable ``.//<tag>`` float across ``tags``, else None."""
    for tag in tags:
        el = root.find(f".//{tag}")
        if el is not None:
            val = _float_or_none(el)
            if val is not None:
                return val
    return None


def _schedule_from_doc_type(doc_type: str, fallback: str) -> tuple[str, bool, bool]:
    """Derive ``(schedule, is_amendment, is_activist)`` from a documentType.

    ``fallback`` (``"13D"``/``"13G"``) is used when ``doc_type`` is silent.
    """
    up = doc_type.upper()
    if "13D" in up:
        schedule = "13D"
    elif "13G" in up:
        schedule = "13G"
    else:
        schedule = fallback
    is_amendment = "/A" in up
    is_activist = schedule == "13D"
    return schedule, is_amendment, is_activist


def _transaction_code(percent_of_class: float | None, is_amendment: bool) -> str:
    """``"S"`` when an amendment reduces the stake below threshold, else ``"P"``."""
    if (
        is_amendment
        and percent_of_class is not None
        and percent_of_class < _REPORTING_THRESHOLD_PCT
    ):
        return "S"
    return "P"


# ---------------------------------------------------------------------------
# Modern SEC structured 13D/13G schema (schemaVersion X02xx, default namespace
# http://www.sec.gov/edgar/schedule13D).  Rolled out ~2024-2025; ALL recent
# 13D/13G filings use it.  It differs from the legacy ownershipDocument-style
# layout in three ways the old parser cannot handle:
#   1. a DEFAULT XML namespace, so ``root.find(".//tag")`` matches nothing;
#   2. different tag names (submissionType / reportingPersonCIK / issuerCIK /
#      issuerCusipNumber) nested under formData/coverPageHeader + reportingPersons;
#   3. ``dateOfEvent`` in MM/DD/YYYY (the legacy parser only accepts ISO and
#      raised ValueError → the filing was silently dropped).
# These helpers match by LOCAL tag name (namespace-agnostic), document order.
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    """Strip any ``{namespace}`` prefix from an ElementTree tag."""
    return tag.split("}")[-1]


def _x02_first_text(root: ET.Element, *local_names: str) -> str:
    """First non-empty text whose LOCAL tag name matches, trying names in order."""
    for name in local_names:
        for el in root.iter():
            if _local(el.tag) == name and (el.text or "").strip():
                return el.text.strip()
    return ""


def _mdy_to_iso(value: str) -> str:
    """Convert ``MM/DD/YYYY`` to ``YYYY-MM-DD``; pass ISO/empty through unchanged."""
    s = (value or "").strip()
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return s


def _is_x02_schema(root: ET.Element) -> bool:
    """True for the modern structured 13D schema (has formData/reportingPersonInfo)."""
    for el in root.iter():
        if _local(el.tag) in ("formData", "reportingPersonInfo"):
            return True
    return False


def _parse_structured_x02(
    root: ET.Element, ticker: str, accession: str, schedule_hint: str
) -> list[dict[str, Any]]:
    """Parse a modern (X02xx) structured 13D/13G document into one row dict."""
    submission_type = _x02_first_text(root, "submissionType", "documentType")
    schedule, is_amendment, is_activist = _schedule_from_doc_type(
        submission_type, schedule_hint
    )

    # Lead reporting person = the filer (first reportingPersonInfo block).
    person_id = _x02_first_text(root, "reportingPersonCIK")
    person_name = _x02_first_text(root, "reportingPersonName")

    # Subject (issuer) — the company being acquired.
    subject_cik = _x02_first_text(root, "issuerCIK") or None
    subject_name = _x02_first_text(root, "issuerName") or None
    cusip = _x02_first_text(root, "issuerCusipNumber", "cusip", "cusipNumber") or None

    def _num(*names: str) -> float | None:
        raw = _x02_first_text(root, *names)
        try:
            return float(raw) if raw else None
        except ValueError:
            return None

    percent_of_class = _num("percentOfClass", "aggregatePercent")
    aggregate_amount = _num("aggregateAmountOwned", "aggregateAmount")

    # dateOfEvent is the (recent) event date; fall back to a signature date.
    event_iso = _mdy_to_iso(_x02_first_text(root, "dateOfEvent"))
    filing_iso = _mdy_to_iso(_x02_first_text(root, "signatureDate", "filingDate"))
    filing_ts = _parse_filing_ts(event_iso, filing_iso)

    transaction_code = _transaction_code(percent_of_class, is_amendment)

    return [{
        "ticker": ticker,
        "person_id": person_id,
        "person_name": person_name,
        "filing_ts": filing_ts,
        "schedule": schedule,
        "is_amendment": is_amendment,
        "is_activist": is_activist,
        "percent_of_class": percent_of_class,
        "aggregate_amount": aggregate_amount,
        "cusip": cusip,
        "subject_name": subject_name,
        "subject_cik": subject_cik,
        "transaction_code": transaction_code,
        "txn_idx": 0,
        "accession": accession,
        "is_10b5_1": False,
    }]


def _parse_structured(
    xml_text: str, ticker: str, accession: str, schedule_hint: str
) -> list[dict[str, Any]]:
    """Parse a structured 13D/13G XML document. Raises ET.ParseError on bad XML."""
    root = ET.fromstring(xml_text)

    # Modern schema (X02xx) — different namespace/tags/date format.
    if _is_x02_schema(root):
        return _parse_structured_x02(root, ticker, accession, schedule_hint)

    doc_type = _find_text(root, "documentType")
    schedule, is_amendment, is_activist = _schedule_from_doc_type(
        doc_type, schedule_hint
    )

    # Reporting / filing person (the activist) — CIK + name.
    person_id = _find_text(
        root, "rptOwnerCik", "filingPersonCik", "reportingPersonCik", "filerCik"
    )
    person_name = _find_text(
        root, "rptOwnerName", "filingPersonName", "reportingPersonName", "filerName"
    )

    percent_of_class = _find_float_or_none(
        root, "percentOfClass", "percentOwned", "aggregatePercent"
    )
    aggregate_amount = _find_float_or_none(
        root, "aggregateAmountOwned", "aggregateAmount", "amountBeneficiallyOwned"
    )
    cusip = _find_text(root, "cusip", "cusipNumber") or None
    # Subject (issuer) name + CIK — used to resolve a ticker when the filing
    # was discovered by filer CIK (no subject ticker known up front).  CIK is
    # the reliable key (exact reverse lookup); name is the CUSIP-match fallback.
    subject_name = _find_text(
        root, "issuerName", "nameOfIssuer", "subjectCompanyName", "issuerNm"
    ) or None
    subject_cik = _find_text(
        root, "issuerCik", "subjectCompanyCik", "cikOfIssuer", "issuerCIK"
    ) or None

    event_date = _find_text(
        root, "dateOfEvent", "eventDate", "dateOfEventRequiringFiling"
    )
    filing_date = _find_text(root, "filingDate", "signatureDate", "periodOfReport")
    filing_ts = _parse_filing_ts(event_date, filing_date)

    transaction_code = _transaction_code(percent_of_class, is_amendment)

    row: dict[str, Any] = {
        "ticker": ticker,
        "person_id": person_id,
        "person_name": person_name,
        "filing_ts": filing_ts,
        "schedule": schedule,
        "is_amendment": is_amendment,
        "is_activist": is_activist,
        "percent_of_class": percent_of_class,
        "aggregate_amount": aggregate_amount,
        "cusip": cusip,
        "subject_name": subject_name,
        "subject_cik": subject_cik,
        "transaction_code": transaction_code,
        "txn_idx": 0,
        "accession": accession,
        "is_10b5_1": False,
    }
    return [row]


def _ymd_to_iso(yyyymmdd: str) -> str:
    """Convert an 8-digit ``YYYYMMDD`` to ``YYYY-MM-DD`` (empty -> empty)."""
    if len(yyyymmdd) == 8 and yyyymmdd.isdigit():
        return f"{yyyymmdd[0:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"
    return ""


def _parse_header_only(
    raw_text: str, ticker: str, accession: str, schedule_hint: str
) -> list[dict[str, Any]]:
    """Parse an old-style plain-text ``<SEC-HEADER>`` 13D/13G filing."""
    sub_match = _HDR_SUBMISSION_TYPE_RE.search(raw_text)
    doc_type = sub_match.group(1) if sub_match else ""
    schedule, is_amendment, is_activist = _schedule_from_doc_type(
        doc_type, schedule_hint
    )

    # Filing person ("FILED BY" block) — name + CIK.
    person_name = ""
    person_id = ""
    filed_by_idx = re.search(r"FILED\s+BY", raw_text, re.IGNORECASE)
    search_region = raw_text[filed_by_idx.start():] if filed_by_idx else raw_text

    # Subject (issuer) name — the "SUBJECT COMPANY" block precedes "FILED BY".
    # Used to resolve a ticker for filer-CIK-discovered filings.
    subject_name = None
    subject_cik = None
    subject_region = raw_text[:filed_by_idx.start()] if filed_by_idx else raw_text
    subj_idx = re.search(r"SUBJECT\s+COMPANY", subject_region, re.IGNORECASE)
    if subj_idx:
        subj_tail = subject_region[subj_idx.start():]
        subj_name_m = re.search(
            r"COMPANY\s+CONFORMED\s+NAME:\s*(.+)", subj_tail, re.IGNORECASE
        )
        if subj_name_m:
            subject_name = subj_name_m.group(1).strip()
        subj_cik_m = re.search(
            r"CENTRAL\s+INDEX\s+KEY:\s*([0-9]+)", subj_tail, re.IGNORECASE
        )
        if subj_cik_m:
            subject_cik = subj_cik_m.group(1).strip().zfill(10)
    name_m = re.search(
        r"COMPANY\s+CONFORMED\s+NAME:\s*(.+)", search_region, re.IGNORECASE
    )
    if name_m:
        person_name = name_m.group(1).strip()
    cik_m = re.search(r"CENTRAL\s+INDEX\s+KEY:\s*([0-9]+)", search_region, re.IGNORECASE)
    if cik_m:
        person_id = cik_m.group(1).strip().zfill(10)

    # CUSIP + percent.
    cusip_m = _HDR_CUSIP_RE.search(raw_text)
    cusip = cusip_m.group(1) if cusip_m else None

    pct_m = _PERCENT_OF_CLASS_RE.search(raw_text)
    if pct_m is None:
        pct_m = _ANY_PERCENT_RE.search(raw_text)
    percent_of_class = float(pct_m.group(1)) if pct_m else None

    # Filing date.
    date_m = _HDR_FILED_AS_OF_RE.search(raw_text)
    filing_date = _ymd_to_iso(date_m.group(1)) if date_m else ""
    if not filing_date:
        return []
    filing_ts = _parse_filing_ts("", filing_date)

    transaction_code = _transaction_code(percent_of_class, is_amendment)

    row: dict[str, Any] = {
        "ticker": ticker,
        "person_id": person_id,
        "person_name": person_name,
        "filing_ts": filing_ts,
        "schedule": schedule,
        "is_amendment": is_amendment,
        "is_activist": is_activist,
        "percent_of_class": percent_of_class,
        "aggregate_amount": None,
        "cusip": cusip,
        "subject_name": subject_name,
        "subject_cik": subject_cik,
        "transaction_code": transaction_code,
        "txn_idx": 0,
        "accession": accession,
        "is_10b5_1": False,
    }
    return [row]


def parse_sc13(
    raw_text: str,
    ticker: str,
    accession: str,
    *,
    schedule: str,
) -> list[dict[str, Any]]:
    """Parse a 13D/13G document into a list of (one) raw filing dict.

    Parameters
    ----------
    raw_text:
        Raw document body fetched from EDGAR (structured XML or plain text).
    ticker:
        Subject-company ticker (the target we trade).
    accession:
        EDGAR accession number, e.g. ``"0001234567-26-000001"``.
    schedule:
        ``"13D"`` or ``"13G"`` discovery hint (used when the document type is
        silent; the parsed ``<documentType>`` takes precedence).

    Returns
    -------
    A list with a single row dict (one row per filing), or ``[]`` on
    malformed/empty input.  Never raises.
    """
    if not raw_text or not raw_text.strip():
        return []

    schedule_hint = "13D" if "13D" in (schedule or "").upper() else "13G"

    stripped = raw_text.lstrip()
    looks_like_xml = stripped.startswith("<?xml") or stripped.startswith("<")

    if looks_like_xml:
        try:
            return _parse_structured(raw_text, ticker, accession, schedule_hint)
        except ET.ParseError:
            # Fall through to the header-only path (some .txt wrappers begin
            # with an XML-ish preamble but are really plain text).
            pass
        except ValueError:
            # Structured XML parsed but carried no usable date (or another
            # value error). It is unusable; never raise on hostile input.
            return []

    try:
        return _parse_header_only(raw_text, ticker, accession, schedule_hint)
    except (ValueError, AttributeError):
        return []
