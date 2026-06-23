"""End-to-end fixture test for the Congress PTR pipeline — Layer L5 (integration).

Tests the full L1→L2→L3→L4 chain:
    fake CongressClient (returns fixture bytes)
    → parse_index / filter_ptrs  (L2)
    → extract_ptr_text / parse_ptr (L3, with real fixture PDFs)
    → to_raw_filings             (L4)
    → fetch_house_ptrs            (L5 orchestrator)

NO real network calls.  The fake client is injected via the ``http_client``
constructor parameter on ``CongressClient`` or via a thin subclass that
overrides ``fetch_house_index`` / ``fetch_ptr_pdf`` directly.

Fixture data
-----------
- ``2026FD_index_sample.txt``     — real 2026 index rows (TAB-delimited)
- ``ptr_20033751.pdf``            — Allen: FERG purchase, NFLX sale (SP owner)
- ``ptr_20034201.pdf``            — Alford: AMZN/AAPL/T/BRK.B partial sales

Expected RawFiling outputs
--------------------------
Allen (20033751):
    - FERG / P / amount_low=15_001 / amount_high=50_000
    - NFLX / S / amount_low=1_001  / amount_high=15_000
Alford (20034201):
    - AMZN / S (partial) / amount_low=1_001 / amount_high=15_000
    - AAPL / S (partial)
    - T    / S (partial)
    - BRK.B / S (partial)   (BRK/B normalised to BRK.B)
    ... plus any additional tickers parsed from that PTR

Design constraints
------------------
- ``from __future__ import annotations``
- No ``datetime.now()``
- [C1] House filing_ts = Clerk receipt date (index FilingDate, public-availability),
  as a tz-aware UTC ISO string — NOT the earlier per-row PDF notification date.
"""
from __future__ import annotations

import io
import pathlib
import zipfile
from datetime import date, timezone, datetime

import pytest

from arbiter.ingest.congress import (
    CongressClient,
    fetch_house_ptrs,
    fetch_and_normalize_house,
)
from arbiter.ingest.congress.normalize import RawFiling

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "congress"


# ---------------------------------------------------------------------------
# Helpers: build an in-memory House index zip from the fixture .txt file
# ---------------------------------------------------------------------------

def _build_index_zip(year: int = 2026) -> bytes:
    """Wrap the real 2026FD_index_sample.txt fixture inside a zip in memory."""
    txt_path = FIXTURE_DIR / f"{year}FD_index_sample.txt"
    txt_bytes = txt_path.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{year}FD.txt", txt_bytes)
    return buf.getvalue()


def _load_pdf(doc_id: str) -> bytes:
    return (FIXTURE_DIR / f"ptr_{doc_id}.pdf").read_bytes()


# ---------------------------------------------------------------------------
# Fake CongressClient — overrides only the two HTTP methods used by the pipeline
# ---------------------------------------------------------------------------

class _FakeCongressClient(CongressClient):
    """Subclass that short-circuits fetch_house_index and fetch_ptr_pdf.

    Constructor accepts:
    - ``index_zip_bytes``: bytes returned by fetch_house_index()
    - ``pdf_map``:         {doc_id: bytes} returned by fetch_ptr_pdf()
    """

    def __init__(
        self,
        index_zip_bytes: bytes,
        pdf_map: dict[str, bytes],
    ) -> None:
        # Do NOT call super().__init__() — we don't want a real httpx.Client
        self._index_zip_bytes = index_zip_bytes
        self._pdf_map = pdf_map
        self._fetched_doc_ids: list[str] = []

    def fetch_house_index(self, year: int) -> bytes:  # noqa: D102
        return self._index_zip_bytes

    def fetch_ptr_pdf(self, year: int, doc_id: str) -> bytes:  # noqa: D102
        self._fetched_doc_ids.append(doc_id)
        if doc_id not in self._pdf_map:
            from arbiter.ingest.congress.client import CongressFetchError
            raise CongressFetchError(
                f"No fixture PDF for doc_id={doc_id!r}",
                url=f"fake://{doc_id}.pdf",
                status_code=404,
            )
        return self._pdf_map[doc_id]


# ---------------------------------------------------------------------------
# Module-level fixture data
# ---------------------------------------------------------------------------

YEAR = 2026
# Build the zip once for the whole module
_INDEX_ZIP = _build_index_zip(YEAR)
_PDF_MAP = {
    "20033751": _load_pdf("20033751"),
    "20034201": _load_pdf("20034201"),
}


# ---------------------------------------------------------------------------
# Shared pytest fixture: run the full pipeline with the 2 real PDFs
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def all_filings() -> list[RawFiling]:
    """Run fetch_house_ptrs over the two fixture PTRs and return all RawFilings."""
    client = _FakeCongressClient(
        index_zip_bytes=_INDEX_ZIP,
        pdf_map=_PDF_MAP,
    )
    # Limit to the two PDFs we have fixtures for; the others would 404.
    # The fake client will raise CongressFetchError for any doc_id not in _PDF_MAP,
    # and fetch_house_ptrs is fault-isolated so those are skipped gracefully.
    # We set limit high enough to reach our two target doc_ids in the index.
    filings = fetch_house_ptrs(client, YEAR, limit=100)
    return filings


@pytest.fixture(scope="module")
def allen_filings(all_filings) -> list[RawFiling]:
    """Filings attributed to Richard W. Allen (doc_id 20033751)."""
    return [f for f in all_filings if "Allen" in f["person_name"]]


@pytest.fixture(scope="module")
def alford_filings(all_filings) -> list[RawFiling]:
    """Filings attributed to Mark Alford (doc_id 20034201)."""
    return [f for f in all_filings if "Alford" in f["person_name"]]


def _by_ticker(filings: list[RawFiling], ticker: str) -> RawFiling:
    matches = [f for f in filings if f["ticker"] == ticker]
    assert matches, f"No filing with ticker={ticker!r}; found: {[f['ticker'] for f in filings]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Basic pipeline smoke test
# ---------------------------------------------------------------------------

class TestPipelineSmoke:
    """Sanity checks: pipeline returns results, source is correct, no network."""

    def test_returns_filings(self, all_filings):
        assert len(all_filings) >= 1, "Pipeline must produce at least one RawFiling"

    def test_source_is_congress(self, all_filings):
        for f in all_filings:
            assert f["source"] == "congress", f"source must be 'congress', got {f['source']!r}"

    def test_shares_always_none(self, all_filings):
        for f in all_filings:
            assert f["shares"] is None, "Congress does not disclose share count"

    def test_price_always_none(self, all_filings):
        for f in all_filings:
            assert f["price"] is None, "Congress does not disclose per-share price"

    def test_is_10b5_1_always_false(self, all_filings):
        for f in all_filings:
            assert f["is_10b5_1"] is False

    def test_filing_ts_is_tz_aware(self, all_filings):
        for f in all_filings:
            dt = datetime.fromisoformat(f["filing_ts"])
            assert dt.tzinfo is not None, f"filing_ts must be tz-aware: {f['filing_ts']!r}"

    def test_filing_ts_is_utc(self, all_filings):
        from datetime import timedelta
        for f in all_filings:
            dt = datetime.fromisoformat(f["filing_ts"])
            assert dt.utcoffset() == timedelta(0), "filing_ts must be UTC"

    def test_accession_present(self, all_filings):
        for f in all_filings:
            assert f.get("accession"), "accession must be non-empty"

    def test_accession_format(self, all_filings):
        """Synthetic accession must follow H-{doc_id}-{txn_idx} format."""
        for f in all_filings:
            acc = f["accession"]
            assert acc.startswith("H-"), f"accession must start with 'H-': {acc!r}"

    def test_txn_type_only_p_or_s(self, all_filings):
        for f in all_filings:
            assert f["txn_type"] in {"P", "S"}, (
                f"txn_type must be P or S, got {f['txn_type']!r}"
            )

    def test_amount_range_not_midpoint(self, all_filings):
        """amount_low and amount_high must be the raw bracket bounds (never imputed)."""
        for f in all_filings:
            if f["amount_low"] != f["amount_high"]:  # skip Over-50M sentinel
                mid = (f["amount_low"] + f["amount_high"]) / 2
                assert f["amount_low"] != mid, "amount_low must NOT be the midpoint"
                assert f["amount_high"] != mid, "amount_high must NOT be the midpoint"


# ---------------------------------------------------------------------------
# Allen 20033751 — FERG purchase + NFLX sale
# ---------------------------------------------------------------------------

class TestAllenFilings:
    """ptr_20033751.pdf: Richard W. Allen — FERG buy, NFLX sale, SP owner."""

    def test_has_filings(self, allen_filings):
        assert len(allen_filings) >= 2, (
            f"Allen must have at least 2 filings; got {len(allen_filings)}: "
            f"{[(f['ticker'], f['txn_type']) for f in allen_filings]}"
        )

    def test_person_name(self, allen_filings):
        for f in allen_filings:
            assert "Allen" in f["person_name"], f"person_name mismatch: {f['person_name']!r}"

    # FERG purchase
    def test_ferg_ticker(self, allen_filings):
        f = _by_ticker(allen_filings, "FERG")
        assert f["ticker"] == "FERG"

    def test_ferg_txn_type_purchase(self, allen_filings):
        f = _by_ticker(allen_filings, "FERG")
        assert f["txn_type"] == "P"

    def test_ferg_amount_low(self, allen_filings):
        f = _by_ticker(allen_filings, "FERG")
        assert f["amount_low"] == 15_001.0

    def test_ferg_amount_high(self, allen_filings):
        f = _by_ticker(allen_filings, "FERG")
        assert f["amount_high"] == 50_000.0

    def test_ferg_filing_ts_is_clerk_receipt_date(self, allen_filings):
        """[C1] filing_ts must be the Clerk receipt / public-availability date.

        For Allen (doc 20033751) the index FilingDate is 01/15/2026 — that is
        when the disclosure became PUBLIC. The PDF per-row notification date
        (01/06/2026) is EARLIER (when the member was notified); using it would
        be look-ahead. filing_ts must be the later, public date.
        """
        f = _by_ticker(allen_filings, "FERG")
        # Clerk receipt date: 01/15/2026 → 2026-01-15
        assert "2026-01-15" in f["filing_ts"], (
            f"filing_ts must contain Clerk receipt date 2026-01-15, got {f['filing_ts']!r}"
        )
        # The earlier per-row notification date must NOT be used (look-ahead).
        assert "2026-01-06" not in f["filing_ts"]
        # Trade date 12/12/2025 must NOT appear
        assert "2025-12-12" not in f["filing_ts"]

    # NFLX sale
    def test_nflx_ticker(self, allen_filings):
        f = _by_ticker(allen_filings, "NFLX")
        assert f["ticker"] == "NFLX"

    def test_nflx_txn_type_sale(self, allen_filings):
        f = _by_ticker(allen_filings, "NFLX")
        assert f["txn_type"] == "S"

    def test_nflx_amount_low(self, allen_filings):
        f = _by_ticker(allen_filings, "NFLX")
        assert f["amount_low"] == 1_001.0

    def test_nflx_amount_high(self, allen_filings):
        f = _by_ticker(allen_filings, "NFLX")
        assert f["amount_high"] == 15_000.0

    def test_nflx_filing_ts_is_clerk_receipt_date(self, allen_filings):
        """[C1] filing_ts = Clerk receipt date 01/15/2026, not the earlier 01/06 notification."""
        f = _by_ticker(allen_filings, "NFLX")
        assert "2026-01-15" in f["filing_ts"]
        assert "2026-01-06" not in f["filing_ts"]


# ---------------------------------------------------------------------------
# Alford 20034201 — AMZN/AAPL/T/BRK.B partial sales
# ---------------------------------------------------------------------------

class TestAlfordFilings:
    """ptr_20034201.pdf: Mark Alford — AMZN/AAPL/T/BRK.B partial sales."""

    def test_has_filings(self, alford_filings):
        assert len(alford_filings) >= 4, (
            f"Alford must have at least 4 filings; got {len(alford_filings)}: "
            f"{[(f['ticker'], f['txn_type']) for f in alford_filings]}"
        )

    def test_person_name(self, alford_filings):
        for f in alford_filings:
            assert "Alford" in f["person_name"]

    def _assert_partial_sale(self, f: RawFiling, ticker: str):
        assert f["txn_type"] == "S", f"{ticker}: expected txn_type='S', got {f['txn_type']!r}"
        assert f["amount_low"] == 1_001.0, f"{ticker}: amount_low mismatch"
        assert f["amount_high"] == 15_000.0, f"{ticker}: amount_high mismatch"
        # [C1] Clerk receipt / public-availability date 03/31/2026 (index FilingDate),
        # NOT the earlier per-row PDF notification date 03/16/2026 (look-ahead).
        assert "2026-03-31" in f["filing_ts"], (
            f"{ticker}: filing_ts must contain Clerk receipt date 2026-03-31, got {f['filing_ts']!r}"
        )
        assert "2026-03-16" not in f["filing_ts"]

    def test_amzn(self, alford_filings):
        f = _by_ticker(alford_filings, "AMZN")
        self._assert_partial_sale(f, "AMZN")

    def test_aapl(self, alford_filings):
        f = _by_ticker(alford_filings, "AAPL")
        self._assert_partial_sale(f, "AAPL")

    def test_att(self, alford_filings):
        f = _by_ticker(alford_filings, "T")
        self._assert_partial_sale(f, "T")

    def test_brkb_normalized(self, alford_filings):
        """BRK/B must be normalized to BRK.B (slash → dot)."""
        f = _by_ticker(alford_filings, "BRK.B")
        self._assert_partial_sale(f, "BRK.B")
        # Must NOT appear as BRK/B
        assert not any(f2["ticker"] == "BRK/B" for f2 in alford_filings), (
            "BRK/B must be normalized to BRK.B"
        )

    def test_filing_ts_is_clerk_receipt_not_trade(self, alford_filings):
        """[C1] All filings: filing_ts must be the Clerk receipt date (2026-03-31).

        That is the index FilingDate (public-availability), not the earlier
        per-row PDF notification date (2026-03-16) which would be look-ahead.
        """
        for f in alford_filings:
            assert "2026-03-31" in f["filing_ts"], (
                f"filing_ts must be the Clerk receipt date 2026-03-31, got {f['filing_ts']!r}"
            )
            assert "2026-03-16" not in f["filing_ts"]


# ---------------------------------------------------------------------------
# fetch_and_normalize_house alias
# ---------------------------------------------------------------------------

class TestAlias:
    """fetch_and_normalize_house must be an alias for fetch_house_ptrs."""

    def test_alias_returns_same_result(self):
        client = _FakeCongressClient(index_zip_bytes=_INDEX_ZIP, pdf_map=_PDF_MAP)
        result = fetch_and_normalize_house(client, YEAR, limit=100)
        assert isinstance(result, list)
        assert len(result) >= 1


# ---------------------------------------------------------------------------
# Fault isolation: unknown doc_id is skipped, rest succeeds
# ---------------------------------------------------------------------------

class TestFaultIsolation:
    """Verify that a bad PTR (404 fetch) is skipped without aborting the pipeline."""

    def test_missing_pdf_skipped_gracefully(self):
        """A 404 for one doc_id must not prevent other PTRs from being processed."""
        # Only provide one of the two PDFs
        client = _FakeCongressClient(
            index_zip_bytes=_INDEX_ZIP,
            pdf_map={"20033751": _PDF_MAP["20033751"]},  # 20034201 will 404
        )
        filings = fetch_house_ptrs(client, YEAR, limit=100)
        # Must still get Allen's filings
        assert len(filings) >= 1
        assert any(f["ticker"] == "FERG" for f in filings)

    def test_empty_pdf_map_returns_empty_list(self):
        """No available PDFs → empty result (not a crash)."""
        client = _FakeCongressClient(index_zip_bytes=_INDEX_ZIP, pdf_map={})
        filings = fetch_house_ptrs(client, YEAR, limit=100)
        assert filings == []


# ---------------------------------------------------------------------------
# Limit parameter
# ---------------------------------------------------------------------------

class TestLimit:
    def test_limit_zero_returns_empty(self):
        client = _FakeCongressClient(index_zip_bytes=_INDEX_ZIP, pdf_map=_PDF_MAP)
        filings = fetch_house_ptrs(client, YEAR, limit=0)
        assert filings == []

    def test_limit_one_fetches_at_most_one_ptr(self):
        client = _FakeCongressClient(index_zip_bytes=_INDEX_ZIP, pdf_map=_PDF_MAP)
        fetch_house_ptrs(client, YEAR, limit=1)
        # Only 1 doc_id should have been attempted
        assert len(client._fetched_doc_ids) <= 1


# ---------------------------------------------------------------------------
# Recency sorting: limit cap must select the MOST RECENT filings
# ---------------------------------------------------------------------------

def _build_synthetic_index_zip(rows: list[dict], year: int = 2026) -> bytes:
    """Build a minimal in-memory index zip from a list of row dicts.

    Each dict must have keys: Last, First, FilingType, StateDst, Year,
    FilingDate (MM/DD/YYYY string), DocID.
    """
    header = "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
    lines = [
        f"Hon.\t{r['Last']}\t{r['First']}\t\t{r['FilingType']}\t"
        f"{r['StateDst']}\t{r['Year']}\t{r['FilingDate']}\t{r['DocID']}\n"
        for r in rows
    ]
    txt = header + "".join(lines)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{year}FD.txt", txt)
    return buf.getvalue()


def _tiny_pdf_bytes(doc_id: str) -> bytes:
    """Return a minimal PDF stub that will fail pdfplumber extraction gracefully.

    We don't need valid parsed transactions here — we just need fetch_house_ptrs
    to *attempt* the fetch for certain doc_ids so we can observe which ones were
    chosen by the recency sort.  A bad PDF is skipped by the fault-isolation
    wrapper, so the call itself is observable via _fetched_doc_ids.
    """
    # Minimal valid PDF-ish payload — pdfplumber may fail, but the HTTP fetch
    # "succeeds" and the doc_id is recorded in _fetched_doc_ids.
    return b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n%%EOF\n"


class TestRecencySorting:
    """fetch_house_ptrs must cap on the MOST RECENT filings, not alphabetically first.

    The House index is ordered by member last name.  Without sorting, limit=N
    would always pick the first N alphabetical entries (A–D members), missing
    late-alphabet members who may have filed more recently.

    Strategy: build a synthetic in-memory index with PTRs spread across a wide
    date range, inject a fake client that tracks which doc_ids are fetched, set
    limit < total records, and assert the fetched doc_ids are the most recent ones.
    """

    # Synthetic PTR rows spanning early-Jan to late-Jun 2026.
    # DocIDs are 8-digit to pass the is_electronic check.
    # Rows are ordered alphabetically by Last (as the real index is) —
    # the most recent filings happen to belong to the Z-surname members.
    _ROWS = [
        # Early filers (alphabetically first — should NOT be picked when limit is small)
        {"Last": "Aaron",   "First": "Alice",  "FilingType": "P", "StateDst": "TX01",
         "Year": "2026", "FilingDate": "1/2/2026",  "DocID": "20010001"},
        {"Last": "Baker",   "First": "Bob",    "FilingType": "P", "StateDst": "CA01",
         "Year": "2026", "FilingDate": "1/5/2026",  "DocID": "20010002"},
        {"Last": "Carter",  "First": "Carol",  "FilingType": "P", "StateDst": "NY01",
         "Year": "2026", "FilingDate": "1/10/2026", "DocID": "20010003"},
        # Mid-year filers
        {"Last": "Davis",   "First": "Dan",    "FilingType": "P", "StateDst": "FL01",
         "Year": "2026", "FilingDate": "3/15/2026", "DocID": "20010004"},
        {"Last": "Evans",   "First": "Eva",    "FilingType": "P", "StateDst": "OH01",
         "Year": "2026", "FilingDate": "4/1/2026",  "DocID": "20010005"},
        # Most recent filers (alphabetically last — these should be chosen when limit is small)
        {"Last": "Young",   "First": "Yara",   "FilingType": "P", "StateDst": "WA01",
         "Year": "2026", "FilingDate": "6/10/2026", "DocID": "20010006"},
        {"Last": "Ziegler", "First": "Zach",   "FilingType": "P", "StateDst": "AZ01",
         "Year": "2026", "FilingDate": "6/17/2026", "DocID": "20010007"},
    ]

    # Most-recent doc_ids (by date desc)
    _NEWEST_DOC_IDS = {"20010007", "20010006"}  # Jun 17, Jun 10
    _OLDEST_DOC_IDS = {"20010001", "20010002"}  # Jan 2, Jan 5

    @classmethod
    def _make_client(cls) -> _FakeCongressClient:
        """Build a fake client with the synthetic index and stub PDFs for all rows."""
        index_zip = _build_synthetic_index_zip(cls._ROWS)
        # All doc_ids return a stub PDF so the fetch "succeeds" (parse may fail — OK)
        pdf_map = {r["DocID"]: _tiny_pdf_bytes(r["DocID"]) for r in cls._ROWS}
        return _FakeCongressClient(index_zip_bytes=index_zip, pdf_map=pdf_map)

    def test_most_recent_fetched_when_limit_smaller_than_total(self):
        """With limit=2 and 7 PTRs, only the 2 most recent doc_ids are fetched."""
        client = self._make_client()
        fetch_house_ptrs(client, 2026, limit=2)
        fetched = set(client._fetched_doc_ids)
        assert fetched == self._NEWEST_DOC_IDS, (
            f"Expected the 2 most recent doc_ids {self._NEWEST_DOC_IDS!r} "
            f"to be fetched; got {fetched!r}"
        )

    def test_oldest_alphabetical_entries_not_fetched_under_small_limit(self):
        """Alphabetically-first entries (Jan filers) must NOT appear when limit is small."""
        client = self._make_client()
        fetch_house_ptrs(client, 2026, limit=2)
        fetched = set(client._fetched_doc_ids)
        overlap = fetched & self._OLDEST_DOC_IDS
        assert not overlap, (
            f"Oldest (alphabetically first) doc_ids {overlap!r} must not be fetched "
            f"when limit=2; recency sort is not working."
        )

    def test_large_limit_fetches_all(self):
        """With limit >= total PTRs, all records are attempted."""
        client = self._make_client()
        fetch_house_ptrs(client, 2026, limit=100)
        fetched = set(client._fetched_doc_ids)
        all_doc_ids = {r["DocID"] for r in self._ROWS}
        assert fetched == all_doc_ids, (
            f"With limit=100 all doc_ids should be fetched; "
            f"missing: {all_doc_ids - fetched!r}"
        )
