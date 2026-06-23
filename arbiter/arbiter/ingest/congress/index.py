from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass
from datetime import date, datetime

logger = logging.getLogger(__name__)

_DATE_FMT = "%m/%d/%Y"
_HEADER_COLS = [
    "Prefix", "Last", "First", "Suffix",
    "FilingType", "StateDst", "Year", "FilingDate", "DocID",
]


@dataclass(frozen=True)
class IndexRecord:
    chamber: str          # "house" | "senate"
    doc_id: str
    filing_type: str      # "P" for PTR
    member_last: str
    member_first: str
    member_suffix: str
    state_dist: str       # e.g. "MO04"
    filing_date: date
    year: int
    is_electronic: bool   # True if doc_id is the 8-digit electronic form


def _is_electronic(doc_id: str) -> bool:
    """8-digit (or more) all-numeric DocID → electronic (text-extractable PDF)."""
    return doc_id.isdigit() and len(doc_id) >= 8


def parse_index(
    zip_bytes: bytes,
    *,
    chamber: str = "house",
    year: int | None = None,
) -> list[IndexRecord]:
    """
    Parse a House financial-disclosure annual index zip.

    Parameters
    ----------
    zip_bytes:
        Raw bytes of e.g. ``2026FD.zip``.
    chamber:
        "house" or "senate". Stored verbatim on every ``IndexRecord``.
    year:
        The 4-digit year used to locate ``{year}FD.txt`` inside the zip.
        If *None*, the year is inferred from the first matching member name
        inside the archive (``\d{4}FD.txt``).

    Returns
    -------
    list[IndexRecord]
        One record per data row; malformed rows are skipped with a WARNING.
    """
    buf = io.BytesIO(zip_bytes)
    records: list[IndexRecord] = []

    with zipfile.ZipFile(buf, "r") as zf:
        names = zf.namelist()

        # Resolve the txt member name.
        if year is not None:
            target = f"{year}FD.txt"
        else:
            # Auto-detect: first member matching NNNNfd.txt (case-insensitive)
            import re
            target = next(
                (n for n in names if re.fullmatch(r"\d{4}FD\.txt", n, re.IGNORECASE)),
                None,
            )
            if target is None:
                logger.error("No NNNNFD.txt member found in zip. Members: %s", names)
                return records

        if target not in names:
            # Some zips ship the txt with a path prefix — try suffix match
            candidates = [n for n in names if n.endswith(target)]
            if not candidates:
                logger.error(
                    "Member %r not found in zip. Available: %s", target, names
                )
                return records
            target = candidates[0]

        raw = zf.read(target)

    # Decode (UTF-8, fall back to latin-1 for old files)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")

    lines = text.splitlines()
    if not lines:
        logger.warning("Index file %r is empty.", target)
        return records

    # First line is the header — skip it (but verify columns if present)
    data_lines = lines[1:]

    for lineno, line in enumerate(data_lines, start=2):
        line = line.rstrip("\r\n")
        if not line.strip():
            continue  # blank line

        parts = line.split("\t")

        # We need at least 9 tab-delimited fields.
        if len(parts) < 9:
            logger.warning(
                "Row %d has only %d fields (need 9) — skipped: %r",
                lineno, len(parts), line,
            )
            continue

        (
            _prefix,
            last,
            first,
            suffix,
            filing_type,
            state_dst,
            year_raw,
            filing_date_raw,
            doc_id,
            *_extra,
        ) = parts

        # Strip whitespace from all fields; suffix may be empty string.
        last = last.strip()
        first = first.strip()
        suffix = suffix.strip()
        filing_type = filing_type.strip()
        state_dst = state_dst.strip()
        year_raw = year_raw.strip()
        filing_date_raw = filing_date_raw.strip()
        doc_id = doc_id.strip()

        # Validate required fields
        if not last or not filing_type or not doc_id:
            logger.warning(
                "Row %d missing required field(s) — skipped: %r", lineno, line
            )
            continue

        # Parse year
        try:
            row_year = int(year_raw)
        except ValueError:
            logger.warning(
                "Row %d: invalid Year %r — skipped: %r", lineno, year_raw, line
            )
            continue

        # Parse filing date
        try:
            parsed_date = datetime.strptime(filing_date_raw, _DATE_FMT).date()
        except ValueError:
            logger.warning(
                "Row %d: invalid FilingDate %r — skipped: %r",
                lineno, filing_date_raw, line,
            )
            continue

        records.append(
            IndexRecord(
                chamber=chamber,
                doc_id=doc_id,
                filing_type=filing_type,
                member_last=last,
                member_first=first,
                member_suffix=suffix,
                state_dist=state_dst,
                filing_date=parsed_date,
                year=row_year,
                is_electronic=_is_electronic(doc_id),
            )
        )

    return records


# House FilingType codes that represent PTR AMENDMENTS / corrections rather than
# an original PTR ("P"). The Clerk uses "C" for amended/corrected PTR filings.
# These correct a previously filed PTR; silently dropping them leaves the stale
# original active. We surface them as an explicit, logged gap (see [C3] below).
_PTR_AMENDMENT_FILING_TYPES: frozenset[str] = frozenset({"C"})


def filter_ptrs(
    records: list[IndexRecord],
    *,
    electronic_only: bool = False,
) -> list[IndexRecord]:
    """
    Return only original Periodic Transaction Reports (FilingType == "P").

    Parameters
    ----------
    records:
        Full list from ``parse_index``.
    electronic_only:
        If True, also restrict to ``is_electronic == True`` (parseable PDFs).

    [C3] House PTR amendments / corrections
    ---------------------------------------
    The House index also contains PTR *amendments* (FilingType ``"C"``) which
    correct a previously filed PTR. This function returns only originals
    (``"P"``). Rather than dropping amendments SILENTLY — which would leave the
    stale original active and double-count — we detect them and emit a clear
    WARNING per amendment so the gap is visible and auditable. Full
    amendment-supersession for the House is a documented limitation: the House
    index gives no machine-readable pointer from a ``"C"`` filing back to the
    original PTR's DocID, so they are logged here and excluded from automatic
    ingest (no fabricated supersede) pending a referent-resolution pass.
    """
    out = [r for r in records if r.filing_type == "P"]

    # Detect (do not silently drop) PTR amendments/corrections.
    amendments = [r for r in records if r.filing_type in _PTR_AMENDMENT_FILING_TYPES]
    if amendments:
        logger.warning(
            "filter_ptrs: %d House PTR amendment/correction filing(s) detected and "
            "NOT auto-ingested (FilingType in %s) — stale originals may remain active. "
            "DocIDs: %s",
            len(amendments),
            sorted(_PTR_AMENDMENT_FILING_TYPES),
            [r.doc_id for r in amendments],
        )

    if electronic_only:
        out = [r for r in out if r.is_electronic]
    return out
