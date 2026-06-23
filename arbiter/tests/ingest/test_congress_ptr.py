"""Tests for L3: PTR PDF extraction and parsing.

Loads REAL fixture files (txt and pdf) from tests/ingest/fixtures/congress/.
"""
from __future__ import annotations

import importlib
import pathlib
import sys
from datetime import date

import pytest

# ---------------------------------------------------------------------------
# Import ptr_pdf directly via importlib to avoid any package __init__ shadowing
# that occurs in pytest's import order.  The module itself has no such issues.
# ---------------------------------------------------------------------------
_ptr_pdf = importlib.import_module("arbiter.ingest.congress.ptr_pdf")
PtrText = _ptr_pdf.PtrText
Transaction = _ptr_pdf.Transaction
extract_ptr_text = _ptr_pdf.extract_ptr_text
parse_ptr = _ptr_pdf.parse_ptr

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "congress"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_txt(name: str) -> PtrText:  # type: ignore[valid-type]
    """Read a .txt fixture and wrap it in a PtrText."""
    path = FIXTURE_DIR / name
    raw = path.read_text(encoding="utf-8")
    doc_id = name.replace("ptr_", "").replace(".txt", "")
    return PtrText(raw_text=raw, is_electronic=True, doc_id=doc_id, chamber="house", year=2026)


def _load_pdf_bytes(name: str) -> bytes:
    return (FIXTURE_DIR / name).read_bytes()


def _by_ticker(txns: list, ticker: str):  # type: ignore[type-arg]
    matches = [t for t in txns if t.ticker == ticker]
    assert matches, f"No transaction with ticker={ticker!r}; found: {[t.ticker for t in txns]}"
    return matches[0]


# ---------------------------------------------------------------------------
# Scanned / empty PTR → returns []
# ---------------------------------------------------------------------------

class TestScannedPtr:
    def test_empty_raw_text_returns_empty_list(self):
        ptr = PtrText(raw_text="", is_electronic=False, doc_id="8068", chamber="house", year=2024)
        assert parse_ptr(ptr) == []

    def test_whitespace_only_returns_empty_list(self):
        ptr = PtrText(raw_text="   \n\n  ", is_electronic=False, doc_id="8068", chamber="house", year=2024)
        assert parse_ptr(ptr) == []


# ---------------------------------------------------------------------------
# ptr_20033751 — Allen: FERG purchase + NFLX sale (SP owner)
# ---------------------------------------------------------------------------

class TestAllen20033751:
    @pytest.fixture(scope="class")
    def txns(self):
        ptr = _load_txt("ptr_20033751.txt")
        return parse_ptr(ptr)

    def test_has_two_transactions(self, txns):
        assert len(txns) == 2, (
            f"Expected 2 transactions, got {len(txns)}: "
            f"{[(t.ticker, t.txn_type) for t in txns]}"
        )

    def test_member_name(self, txns):
        for t in txns:
            assert t.member_name == "Richard W. Allen"

    def test_ferg_purchase(self, txns):
        t = _by_ticker(txns, "FERG")
        assert t.txn_type == "P"
        assert t.is_partial is False
        assert t.owner == "SP"
        assert t.amount_low == 15_001.0
        assert t.amount_high == 50_000.0
        assert t.txn_date == date(2025, 12, 12)
        assert t.notification_date == date(2026, 1, 6)
        assert t.asset_type == "ST"

    def test_nflx_sale(self, txns):
        t = _by_ticker(txns, "NFLX")
        assert t.txn_type == "S"
        assert t.is_partial is False
        assert t.owner == "SP"
        assert t.amount_low == 1_001.0
        assert t.amount_high == 15_000.0
        assert t.txn_date == date(2025, 12, 12)
        assert t.notification_date == date(2026, 1, 6)
        assert t.asset_type == "ST"

    def test_doc_id_and_chamber(self, txns):
        for t in txns:
            assert t.doc_id == "20033751"
            assert t.chamber == "house"


# ---------------------------------------------------------------------------
# ptr_20034201 — Alford: AMZN/AAPL/T/BRK.B partial sales (SELF owner)
# ---------------------------------------------------------------------------

class TestAlford20034201:
    @pytest.fixture(scope="class")
    def txns(self):
        ptr = _load_txt("ptr_20034201.txt")
        return parse_ptr(ptr)

    def test_member_name(self, txns):
        for t in txns:
            assert t.member_name == "Mark Alford"

    def _assert_partial_sale(self, t: object, ticker: str) -> None:
        assert t.txn_type == "S", f"{ticker}: expected S got {t.txn_type}"  # type: ignore[attr-defined]
        assert t.is_partial is True, f"{ticker}: expected is_partial=True"  # type: ignore[attr-defined]
        assert t.owner == "SELF", f"{ticker}: expected SELF got {t.owner}"  # type: ignore[attr-defined]
        assert t.amount_low == 1_001.0, f"{ticker}: amount_low mismatch"  # type: ignore[attr-defined]
        assert t.amount_high == 15_000.0, f"{ticker}: amount_high mismatch"  # type: ignore[attr-defined]
        assert t.txn_date == date(2026, 3, 16), f"{ticker}: txn_date mismatch"  # type: ignore[attr-defined]
        assert t.notification_date == date(2026, 3, 16), f"{ticker}: notification_date mismatch"  # type: ignore[attr-defined]

    def test_amzn(self, txns):
        t = _by_ticker(txns, "AMZN")
        self._assert_partial_sale(t, "AMZN")
        assert t.asset_type == "ST"  # type: ignore[attr-defined]

    def test_aapl(self, txns):
        t = _by_ticker(txns, "AAPL")
        self._assert_partial_sale(t, "AAPL")
        assert t.asset_type == "ST"  # type: ignore[attr-defined]

    def test_att(self, txns):
        t = _by_ticker(txns, "T")
        self._assert_partial_sale(t, "T")
        assert t.asset_type == "ST"  # type: ignore[attr-defined]

    def test_brkb_normalized(self, txns):
        t = _by_ticker(txns, "BRK.B")
        self._assert_partial_sale(t, "BRK.B")
        assert t.asset_type == "ST"  # type: ignore[attr-defined]
        # Must NOT appear as BRK/B
        assert not any(t2.ticker == "BRK/B" for t2 in txns)

    def test_doc_id_and_chamber(self, txns):
        for t in txns:
            assert t.doc_id == "20034201"
            assert t.chamber == "house"


# ---------------------------------------------------------------------------
# Audited parser fixes — P1/P2/P3 (fixture 20034201 + synthetic chunks)
# ---------------------------------------------------------------------------

class TestParserFixes:
    """Targeted regression tests for the three audited parser findings."""

    # ---- Fixture-level: 20034201 has QQQ and DIA cases ----

    @pytest.fixture(scope="class")
    def alford_txns(self):
        ptr = _load_txt("ptr_20034201.txt")
        return parse_ptr(ptr)

    # [P1 / P3] Invesco QQQ [OT] — tag in asset-name portion before type token,
    # bare uppercase token immediately before [OT].
    def test_qqq_ticker_captured(self, alford_txns):
        t = _by_ticker(alford_txns, "QQQ")
        assert t.asset_type == "OT", f"Expected OT, got {t.asset_type!r}"
        assert t.txn_type == "S"
        assert t.is_partial is True

    def test_qqq_tag_not_in_asset_name(self, alford_txns):
        t = _by_ticker(alford_txns, "QQQ")
        assert "[OT]" not in t.asset_name, (
            f"[OT] tag leaked into asset_name: {t.asset_name!r}"
        )
        assert t.asset_name == "Invesco QQQ"

    # [P3] NYSEARCA: DIA [OT] — exchange-prefixed unparenthesised ticker
    def test_dia_ticker_captured_via_nysearca(self, alford_txns):
        t = _by_ticker(alford_txns, "DIA")
        assert t.asset_type == "OT", f"Expected OT, got {t.asset_type!r}"
        assert t.txn_type == "S"
        assert t.is_partial is True

    def test_dia_tag_not_in_asset_name(self, alford_txns):
        t = _by_ticker(alford_txns, "DIA")
        assert "[OT]" not in t.asset_name, (
            f"[OT] tag leaked into asset_name: {t.asset_name!r}"
        )

    # ---- Synthetic unit chunks ----

    def _make_ptr(self, text: str) -> object:
        return PtrText(
            raw_text=text,
            is_electronic=True,
            doc_id="synthetic",
            chamber="house",
            year=2026,
        )

    # [P2] Double-paren CUSIP artifact: (91282CJR3)) [GS]
    def test_double_paren_cusip_captured(self):
        raw = (
            "Name: Hon. Test Member\n"
            "Virginia ST 4.00% 8/1/38 (91282CJR3)) [GS] S 03/16/2026 03/16/2026 $1,001 - $15,000\n"
        )
        txns = parse_ptr(self._make_ptr(raw))
        assert len(txns) == 1, f"Expected 1 txn, got {len(txns)}"
        t = txns[0]
        assert t.ticker == "91282CJR3", f"Expected CUSIP, got {t.ticker!r}"
        assert t.asset_type == "GS", f"Expected GS, got {t.asset_type!r}"

    def test_double_paren_gs_tag_not_in_asset_name(self):
        raw = (
            "Name: Hon. Test Member\n"
            "Virginia ST 4.00% 8/1/38 (91282CJR3)) [GS] S 03/16/2026 03/16/2026 $1,001 - $15,000\n"
        )
        txns = parse_ptr(self._make_ptr(raw))
        assert len(txns) == 1
        t = txns[0]
        assert "[GS]" not in t.asset_name, (
            f"[GS] tag leaked into asset_name: {t.asset_name!r}"
        )

    # [P1] [GS] in asset name before type token, no ticker in parens
    def test_gs_tag_in_asset_name_no_parens(self):
        raw = (
            "Name: Hon. Test Member\n"
            "Virginia ST 4.00% 8/1/38 [GS] S 03/16/2026 03/16/2026 $1,001 - $15,000\n"
        )
        txns = parse_ptr(self._make_ptr(raw))
        assert len(txns) == 1
        t = txns[0]
        assert t.asset_type == "GS", f"Expected GS, got {t.asset_type!r}"
        assert "[GS]" not in t.asset_name, (
            f"[GS] leaked into asset_name: {t.asset_name!r}"
        )
        assert t.asset_name == "Virginia ST 4.00% 8/1/38"

    # [P3] Bare QQQ immediately before [OT] (synthetic minimal case)
    def test_bare_ticker_before_tag_qqq(self):
        raw = (
            "Name: Hon. Test Member\n"
            "Invesco QQQ [OT] S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000\n"
        )
        txns = parse_ptr(self._make_ptr(raw))
        assert len(txns) == 1
        t = txns[0]
        assert t.ticker == "QQQ", f"Expected QQQ, got {t.ticker!r}"
        assert t.asset_type == "OT"
        assert "[OT]" not in t.asset_name

    # [P3] NYSEARCA: DIA [OT] (synthetic minimal case)
    def test_nysearca_dia_ticker(self):
        raw = (
            "Name: Hon. Test Member\n"
            "DIA State Street ETF S (partial) 03/16/2026 03/16/2026 $1,001 - $15,000"
            " NYSEARCA: DIA [OT]\n"
        )
        txns = parse_ptr(self._make_ptr(raw))
        assert len(txns) == 1
        t = txns[0]
        assert t.ticker == "DIA", f"Expected DIA, got {t.ticker!r}"
        assert t.asset_type == "OT"


# ---------------------------------------------------------------------------
# extract_ptr_text — real PDF fixtures
# ---------------------------------------------------------------------------

class TestExtractPtrText:
    def test_allen_pdf_produces_nonempty_text(self):
        pdf_bytes = _load_pdf_bytes("ptr_20033751.pdf")
        ptr = extract_ptr_text(pdf_bytes, doc_id="20033751", chamber="house", year=2026)
        assert ptr.is_electronic is True
        assert len(ptr.raw_text) > 50
        # Should contain at least one of the tickers
        assert "FERG" in ptr.raw_text or "NFLX" in ptr.raw_text

    def test_alford_pdf_produces_nonempty_text(self):
        pdf_bytes = _load_pdf_bytes("ptr_20034201.pdf")
        ptr = extract_ptr_text(pdf_bytes, doc_id="20034201", chamber="house", year=2026)
        assert ptr.is_electronic is True
        assert len(ptr.raw_text) > 50
        assert "AMZN" in ptr.raw_text or "AAPL" in ptr.raw_text

    def test_metadata_propagated(self):
        pdf_bytes = _load_pdf_bytes("ptr_20033751.pdf")
        ptr = extract_ptr_text(pdf_bytes, doc_id="20033751", chamber="senate", year=2025)
        assert ptr.doc_id == "20033751"
        assert ptr.chamber == "senate"
        assert ptr.year == 2025

    def test_empty_bytes_returns_not_electronic(self):
        # Invalid PDF bytes → pdfplumber will fail or return no text → not electronic
        ptr = extract_ptr_text(b"\x00\x00\x00\x00", doc_id="bad", chamber="house", year=2026)
        assert ptr.is_electronic is False
        assert ptr.raw_text == ""
