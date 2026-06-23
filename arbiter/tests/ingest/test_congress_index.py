from __future__ import annotations

"""Tests for arbiter.ingest.congress.index (L2 — index parser).

Uses the REAL fixture ``tests/ingest/fixtures/congress/2026FD_index_sample.txt``
wrapped in an in-memory zip so no network calls are needed.
"""

import io
import zipfile
from datetime import date
from pathlib import Path

import pytest

from arbiter.ingest.congress.index import IndexRecord, filter_ptrs, parse_index

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures" / "congress"
INDEX_SAMPLE = FIXTURES / "2026FD_index_sample.txt"


def _make_zip(txt_content: str, member_name: str = "2026FD.txt") -> bytes:
    """Wrap *txt_content* in an in-memory zip as *member_name*."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member_name, txt_content)
    return buf.getvalue()


@pytest.fixture(scope="module")
def sample_zip() -> bytes:
    """Real fixture text wrapped in a zip as ``2026FD.txt``."""
    return _make_zip(INDEX_SAMPLE.read_text(encoding="utf-8"), "2026FD.txt")


@pytest.fixture(scope="module")
def sample_records(sample_zip: bytes) -> list[IndexRecord]:
    return parse_index(sample_zip, chamber="house", year=2026)


# ---------------------------------------------------------------------------
# Basic parsing
# ---------------------------------------------------------------------------


def test_parses_expected_count(sample_records: list[IndexRecord]) -> None:
    """All 59 data rows in the sample should parse successfully."""
    # The sample has a header line + 59 data lines = 60 lines total.
    assert len(sample_records) == 59


def test_chamber_set_correctly(sample_records: list[IndexRecord]) -> None:
    assert all(r.chamber == "house" for r in sample_records)


# ---------------------------------------------------------------------------
# IndexRecord field correctness — spot-check first row (Alford)
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def alford(sample_records: list[IndexRecord]) -> IndexRecord:
    matches = [r for r in sample_records if r.member_last == "Alford"]
    assert matches, "Expected at least one Alford record"
    return matches[0]


def test_alford_doc_id(alford: IndexRecord) -> None:
    assert alford.doc_id == "20034201"


def test_alford_filing_type(alford: IndexRecord) -> None:
    assert alford.filing_type == "P"


def test_alford_state_dist(alford: IndexRecord) -> None:
    assert alford.state_dist == "MO04"


def test_alford_filing_date(alford: IndexRecord) -> None:
    assert alford.filing_date == date(2026, 3, 31)


def test_alford_year(alford: IndexRecord) -> None:
    assert alford.year == 2026


def test_alford_member_fields(alford: IndexRecord) -> None:
    assert alford.member_first == "Mark"
    assert alford.member_suffix == ""  # no suffix in fixture


# ---------------------------------------------------------------------------
# is_electronic detection
# ---------------------------------------------------------------------------


def test_8digit_docid_is_electronic(sample_records: list[IndexRecord]) -> None:
    """20034201 → 8 digits → is_electronic True."""
    rec = next(r for r in sample_records if r.doc_id == "20034201")
    assert rec.is_electronic is True


def test_short_docid_is_not_electronic(sample_zip: bytes) -> None:
    """A short/paper DocID (e.g. 8068) → is_electronic False."""
    # Inject a synthetic row with a short doc_id into the zip.
    base_txt = INDEX_SAMPLE.read_text(encoding="utf-8")
    extra_row = "Hon.\tTestMember\tJane\t\tP\tTX01\t2026\t1/1/2026\t8068\n"
    augmented = base_txt + extra_row
    z = _make_zip(augmented, "2026FD.txt")
    records = parse_index(z, chamber="house", year=2026)
    short_rec = next((r for r in records if r.doc_id == "8068"), None)
    assert short_rec is not None
    assert short_rec.is_electronic is False


def test_7digit_docid_is_not_electronic(sample_zip: bytes) -> None:
    """A 7-digit DocID is just below the threshold → is_electronic False."""
    base_txt = INDEX_SAMPLE.read_text(encoding="utf-8")
    extra_row = "Hon.\tTestMember\tJohn\t\tP\tCA01\t2026\t2/2/2026\t2003420\n"
    augmented = base_txt + extra_row
    z = _make_zip(augmented, "2026FD.txt")
    records = parse_index(z, chamber="house", year=2026)
    rec = next((r for r in records if r.doc_id == "2003420"), None)
    assert rec is not None
    assert rec.is_electronic is False


# ---------------------------------------------------------------------------
# filter_ptrs
# ---------------------------------------------------------------------------


def test_filter_ptrs_all_p(sample_records: list[IndexRecord]) -> None:
    """filter_ptrs must return ONLY FilingType=='P' records."""
    ptrs = filter_ptrs(sample_records)
    assert ptrs, "Expected at least one PTR"
    assert all(r.filing_type == "P" for r in ptrs)


def test_filter_ptrs_excludes_non_p(sample_zip: bytes) -> None:
    """Rows with FilingType != 'P' must be excluded by filter_ptrs."""
    base_txt = INDEX_SAMPLE.read_text(encoding="utf-8")
    # Add a non-PTR row (Annual Financial Disclosure = 'A')
    extra = "Hon.\tSmith\tBob\t\tA\tNY10\t2026\t4/1/2026\t20099999\n"
    z = _make_zip(base_txt + extra, "2026FD.txt")
    records = parse_index(z, chamber="house", year=2026)
    ptrs = filter_ptrs(records)
    assert all(r.filing_type == "P" for r in ptrs)
    doc_ids = {r.doc_id for r in ptrs}
    assert "20099999" not in doc_ids


def test_filter_ptrs_electronic_only(sample_records: list[IndexRecord]) -> None:
    """electronic_only=True must further restrict to is_electronic==True."""
    ptrs = filter_ptrs(sample_records, electronic_only=True)
    assert all(r.filing_type == "P" for r in ptrs)
    assert all(r.is_electronic for r in ptrs)


# ---------------------------------------------------------------------------
# [C3 #5] House PTR amendments — detected + logged, not silently dropped
# ---------------------------------------------------------------------------


def test_house_amendment_detected_and_logged(
    sample_zip: bytes, caplog
) -> None:
    """A House PTR amendment (FilingType 'C') is NOT returned as a 'P' original,
    but must be detected and logged (not silently swallowed)."""
    import logging

    base_txt = INDEX_SAMPLE.read_text(encoding="utf-8")
    # FilingType 'C' = amended/corrected PTR
    extra = "Hon.\tSmith\tBob\t\tC\tNY10\t2026\t4/1/2026\t20088888\n"
    z = _make_zip(base_txt + extra, "2026FD.txt")
    records = parse_index(z, chamber="house", year=2026)

    with caplog.at_level(logging.WARNING):
        ptrs = filter_ptrs(records)

    # The amendment must NOT leak into the originals list...
    assert "20088888" not in {r.doc_id for r in ptrs}
    # ...but it must be surfaced via a WARNING (visible gap, not silent drop).
    assert any(
        "amendment" in rec.message.lower() and "20088888" in rec.message
        for rec in caplog.records
    ), "House PTR amendment must be detected and logged, not silently dropped"


def test_filter_ptrs_no_amendment_no_warning(
    sample_records: list[IndexRecord], caplog
) -> None:
    """With no amendments present, filter_ptrs must not emit an amendment warning."""
    import logging

    with caplog.at_level(logging.WARNING):
        filter_ptrs(sample_records)
    assert not any(
        "amendment" in rec.message.lower() for rec in caplog.records
    )


# ---------------------------------------------------------------------------
# filing_date parsing
# ---------------------------------------------------------------------------


def test_allen_earliest_filing_date(sample_records: list[IndexRecord]) -> None:
    """Allen's earliest PTR has FilingDate 1/15/2026 → date(2026, 1, 15)."""
    allen = [r for r in sample_records if r.member_last == "Allen"]
    dates = sorted(r.filing_date for r in allen)
    assert dates[0] == date(2026, 1, 15)


def test_beyer_suffix_jr(sample_records: list[IndexRecord]) -> None:
    """Donald Sternoff Beyer Jr — suffix field should be 'Jr'."""
    beyer = next(r for r in sample_records if r.member_last == "Beyer")
    assert beyer.member_suffix == "Jr"


def test_begich_suffix_iii(sample_records: list[IndexRecord]) -> None:
    """Nicholas Begich III — suffix field should be 'III'."""
    begich = next(r for r in sample_records if r.member_last == "Begich")
    assert begich.member_suffix == "III"


# ---------------------------------------------------------------------------
# Robustness — malformed rows
# ---------------------------------------------------------------------------


def test_malformed_row_skipped_not_fatal() -> None:
    """A row with too few tab-delimited fields must be skipped silently."""
    bad_txt = (
        "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
        "Hon.\tGoodMember\tAlice\t\tP\tCA01\t2026\t1/10/2026\t20034999\n"  # good
        "MALFORMED_ROW_WITH_NO_TABS\n"                                       # bad
        "Hon.\tAnotherMember\tBob\t\tP\tNY05\t2026\t2/5/2026\t20035000\n"  # good
    )
    z = _make_zip(bad_txt)
    records = parse_index(z, chamber="house", year=2026)
    # Should have parsed the two good rows, skipped the malformed one.
    assert len(records) == 2
    doc_ids = {r.doc_id for r in records}
    assert "20034999" in doc_ids
    assert "20035000" in doc_ids


def test_missing_member_name_skipped() -> None:
    """A row where Last is empty must be skipped."""
    bad_txt = (
        "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
        "Hon.\t\tAlice\t\tP\tCA01\t2026\t1/10/2026\t20034999\n"   # bad — no last name
        "Hon.\tValid\tMember\t\tP\tCA02\t2026\t3/3/2026\t20035001\n"  # good
    )
    z = _make_zip(bad_txt)
    records = parse_index(z, chamber="house", year=2026)
    assert len(records) == 1
    assert records[0].doc_id == "20035001"


def test_invalid_date_skipped() -> None:
    """A row with an unparseable FilingDate must be skipped."""
    bad_txt = (
        "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
        "Hon.\tBadDate\tJane\t\tP\tTX01\t2026\tNOT_A_DATE\t20035002\n"  # bad date
        "Hon.\tGoodDate\tJohn\t\tP\tTX02\t2026\t6/1/2026\t20035003\n"   # good
    )
    z = _make_zip(bad_txt)
    records = parse_index(z, chamber="house", year=2026)
    assert len(records) == 1
    assert records[0].doc_id == "20035003"


def test_year_auto_detection() -> None:
    """parse_index with year=None should auto-detect the member from the zip."""
    txt = (
        "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
        "Hon.\tAutoYear\tTest\t\tP\tCA01\t2026\t1/1/2026\t20034001\n"
    )
    z = _make_zip(txt, "2026FD.txt")
    records = parse_index(z, chamber="house")  # no year kwarg
    assert len(records) == 1
    assert records[0].year == 2026


def test_member_not_in_zip_returns_empty() -> None:
    """If the expected member is missing from the zip, return [] gracefully."""
    txt = "Prefix\tLast\tFirst\tSuffix\tFilingType\tStateDst\tYear\tFilingDate\tDocID\n"
    z = _make_zip(txt, "2026FD.txt")
    # Ask for year=2025 but zip only has 2026FD.txt
    records = parse_index(z, chamber="house", year=2025)
    assert records == []
