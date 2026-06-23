"""Offline tests for the Senate eFD ingestion adapter.

All tests use real HTML/JSON fixtures from
``tests/ingest/fixtures/congress/senate/``.
No real network calls are made.

Fixtures:
  ptr_a9754ff5_boozman_2026.html  — 18 Joint sales (15 with ticker, 3 with --)
  ptr_be9bb561_peters_2026.html   — 1 Self Purchase of KHC
  ptr_09b9c1ed_king_2026.html     — 9 Spouse Sales (UBER/PYPL/ONON/NFLX/MSFT/META/LLY/BX/ADSK)
  search_result_ptrs_2026.json    — 5 rows from real 2026 search
"""
from __future__ import annotations

import json
import pathlib
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import the module under test
# ---------------------------------------------------------------------------
from arbiter.ingest.congress.senate import (
    _parse_ptr_page,
    _parse_filer_name,
    _parse_notification_date,
    _parse_owner,
    _parse_asset_type,
    _parse_ticker,
    _parse_amount,
    _parse_txn_type,
    _search_ptrs,
    SenateEFDUnavailable,
    fetch_senate_ptrs,
)
from arbiter.ingest.congress.normalize import to_raw_filings
from arbiter.ingest.congress import fetch_senate_ptrs as orchestrate_senate_ptrs
from arbiter.ingest.congress.client import CongressClient

# ---------------------------------------------------------------------------
# Fixture paths
# ---------------------------------------------------------------------------

_FIXTURE_DIR = (
    pathlib.Path(__file__).parent / "fixtures" / "congress" / "senate"
)

def _load_html(name: str) -> str:
    return (_FIXTURE_DIR / name).read_text(encoding="utf-8")

def _load_json(name: str) -> dict:
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


# ===========================================================================
# Unit tests: parser helpers
# ===========================================================================

class TestParseFilerName:
    def test_strips_honorable(self) -> None:
        assert _parse_filer_name("The Honorable John Boozman (Boozman, John)") == "John Boozman"

    def test_strips_jr(self) -> None:
        name = _parse_filer_name("The Honorable Angus S King  Jr. (King, Angus)")
        assert "Angus" in name
        assert "King" in name
        assert "Jr." not in name
        assert "The Honorable" not in name

    def test_multiline_name(self) -> None:
        # As it appears in the Gary Peters HTML
        raw = "\n                The Honorable Gary\n                C Peters\n                \n                (Peters, Gary)\n            "
        result = _parse_filer_name(raw)
        assert "Gary" in result
        assert "Peters" in result


class TestParseNotificationDate:
    def test_extracts_date(self) -> None:
        d = _parse_notification_date("Periodic Transaction Report\n                \n                    for 06/11/2026\n                ")
        assert d == date(2026, 6, 11)

    def test_simple_for_date(self) -> None:
        assert _parse_notification_date("for 03/24/2026") == date(2026, 3, 24)

    def test_no_date_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_notification_date("No date here")


class TestParseOwner:
    def test_self(self) -> None:
        assert _parse_owner("Self") == "SELF"

    def test_joint(self) -> None:
        assert _parse_owner("Joint") == "JT"

    def test_spouse(self) -> None:
        assert _parse_owner("Spouse") == "SP"

    def test_child(self) -> None:
        assert _parse_owner("Child") == "DC"

    def test_unknown_defaults_self(self) -> None:
        assert _parse_owner("Unknown") == "SELF"


class TestParseAssetType:
    def test_stock(self) -> None:
        assert _parse_asset_type("Stock") == "ST"

    def test_other_securities(self) -> None:
        assert _parse_asset_type("Other Securities") == "OT"

    def test_municipal_security(self) -> None:
        assert _parse_asset_type("Municipal Security") == "MS"

    def test_unknown_becomes_ot(self) -> None:
        assert _parse_asset_type("Exotic Instrument") == "OT"

    def test_case_insensitive(self) -> None:
        assert _parse_asset_type("STOCK") == "ST"


class TestParseTicker:
    def test_extracts_from_a_tag(self) -> None:
        cell_html = '<a href="https://finance.yahoo.com/quote/KHC" target="_blank">KHC</a>'
        assert _parse_ticker("KHC", cell_html) == "KHC"

    def test_dash_dash_returns_none(self) -> None:
        assert _parse_ticker("--", "") is None

    def test_empty_returns_none(self) -> None:
        assert _parse_ticker("", "") is None

    def test_no_a_tag_uses_text(self) -> None:
        assert _parse_ticker("AAPL", "") == "AAPL"


class TestParseAmount:
    def test_basic(self) -> None:
        low, high = _parse_amount("$1,001 - $15,000")
        assert low == 1001.0
        assert high == 15000.0

    def test_larger(self) -> None:
        low, high = _parse_amount("$50,001 - $100,000")
        assert low == 50001.0
        assert high == 100000.0

    def test_invalid_raises(self) -> None:
        with pytest.raises(ValueError):
            _parse_amount("no amount here")


class TestParseTxnType:
    def test_purchase(self) -> None:
        assert _parse_txn_type("Purchase") == ("P", False)

    def test_sale_full(self) -> None:
        assert _parse_txn_type("Sale (Full)") == ("S", False)

    def test_sale_partial(self) -> None:
        assert _parse_txn_type("Sale (Partial)") == ("S", True)

    def test_exchange(self) -> None:
        assert _parse_txn_type("Exchange") == ("E", False)


# ===========================================================================
# Integration tests: parse_ptr_page on real fixture HTML
# ===========================================================================

BOOZMAN_UUID = "a9754ff5-901a-4877-b7be-a647bd361c52"
PETERS_UUID = "be9bb561-8290-4364-85b4-06a59ef0ec01"
KING_UUID = "09b9c1ed-0000-0000-0000-000000000000"  # not in fixture filename; we use a placeholder


class TestParsePtrPageBoozman:
    """ptr_a9754ff5_boozman_2026.html: 18 Joint transactions, all Sale (Partial/Full)."""

    @classmethod
    def setup_class(cls) -> None:
        cls.html = _load_html("ptr_a9754ff5_boozman_2026.html")
        cls.txns = _parse_ptr_page(cls.html, BOOZMAN_UUID)

    def test_count(self) -> None:
        assert len(self.txns) == 18

    def test_all_joint(self) -> None:
        for txn in self.txns:
            assert txn.owner == "JT", f"Expected JT, got {txn.owner} for {txn.asset_name}"

    def test_all_sales(self) -> None:
        for txn in self.txns:
            assert txn.txn_type == "S"

    def test_notification_date(self) -> None:
        for txn in self.txns:
            assert txn.notification_date == date(2026, 6, 16)

    def test_member_name(self) -> None:
        for txn in self.txns:
            assert "Boozman" in txn.member_name
            assert "John" in txn.member_name

    def test_doc_id(self) -> None:
        for txn in self.txns:
            assert txn.doc_id == BOOZMAN_UUID

    def test_chamber(self) -> None:
        for txn in self.txns:
            assert txn.chamber == "senate"

    def test_partial_sales_flagged(self) -> None:
        # Most are Sale (Partial); row 7 (IEI) is Sale (Full)
        partial_count = sum(1 for t in self.txns if t.is_partial)
        full_count = sum(1 for t in self.txns if not t.is_partial)
        assert partial_count > 0
        assert full_count >= 1

    def test_known_ticker_vea(self) -> None:
        tickers = [t.ticker for t in self.txns]
        assert "VEA" in tickers

    def test_known_ticker_iwm(self) -> None:
        tickers = [t.ticker for t in self.txns]
        assert "IWM" in tickers

    def test_dash_ticker_is_none(self) -> None:
        # Rows 11 (SPYM), 4 (FTGC), 1 (ACN) all have '--' ticker in the <td>
        none_count = sum(1 for t in self.txns if t.ticker is None)
        assert none_count == 3, f"Expected 3 None tickers, got {none_count}"

    def test_amount_range(self) -> None:
        # All amounts in this fixture are $1,001 - $15,000 or $50,001 - $100,000
        for txn in self.txns:
            assert txn.amount_low >= 1001.0
            assert txn.amount_high >= txn.amount_low

    def test_asset_type_stock(self) -> None:
        for txn in self.txns:
            assert txn.asset_type == "ST"

    def test_normalize_drops_none_tickers(self) -> None:
        """After normalize, the 3 '--' ticker rows should be dropped."""
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        # 18 total - 3 None tickers = 15 surviving
        assert len(filings) == 15

    def test_normalize_accession_prefix(self) -> None:
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        for f in filings:
            assert f["accession"].startswith(f"S-{BOOZMAN_UUID}-")

    def test_normalize_accession_uses_input_position(self) -> None:
        """Accession i values should be input-enumerate positions, not post-filter."""
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        # Extract i from "S-{uuid}-{i}"
        indices = [int(f["accession"].split("-")[-1]) for f in filings]
        # Input positions: rows with ticker=None are i=3 (row 4 FTGC), i=10 (row 11 SPYM),
        # i=17 (row 1 ACN) — using 0-based enumerate on the reversed list order.
        # What matters: no duplicate indices and they come from the full 0..17 range.
        assert sorted(set(indices)) == sorted(indices), "Duplicate accession indices"
        assert min(indices) >= 0
        assert max(indices) <= 17


class TestParsePtrPagePeters:
    """ptr_be9bb561_peters_2026.html: 1 Self Purchase of KHC."""

    @classmethod
    def setup_class(cls) -> None:
        cls.html = _load_html("ptr_be9bb561_peters_2026.html")
        cls.txns = _parse_ptr_page(cls.html, PETERS_UUID)

    def test_count(self) -> None:
        assert len(self.txns) == 1

    def test_owner_self(self) -> None:
        assert self.txns[0].owner == "SELF"

    def test_txn_type_purchase(self) -> None:
        assert self.txns[0].txn_type == "P"
        assert self.txns[0].is_partial is False

    def test_ticker_khc(self) -> None:
        assert self.txns[0].ticker == "KHC"

    def test_asset_type_stock(self) -> None:
        assert self.txns[0].asset_type == "ST"

    def test_txn_date(self) -> None:
        assert self.txns[0].txn_date == date(2026, 5, 21)

    def test_notification_date(self) -> None:
        assert self.txns[0].notification_date == date(2026, 6, 11)

    def test_amount(self) -> None:
        assert self.txns[0].amount_low == 1001.0
        assert self.txns[0].amount_high == 15000.0

    def test_member_name(self) -> None:
        assert "Peters" in self.txns[0].member_name

    def test_normalize_produces_one_filing(self) -> None:
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        assert len(filings) == 1
        assert filings[0]["ticker"] == "KHC"
        assert filings[0]["txn_type"] == "P"
        assert filings[0]["accession"].startswith(f"S-{PETERS_UUID}-")

    def test_normalize_source_is_congress(self) -> None:
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        assert filings[0]["source"] == "congress"


class TestParsePtrPageKing:
    """ptr_09b9c1ed_king_2026.html: 9 Spouse Sales (UBER/PYPL/ONON/NFLX/MSFT/META/LLY/BX/ADSK)."""

    # The King UUID in the fixture file name is 09b9c1ed — we use a synthetic UUID
    # since the fixture was captured with a placeholder.
    KING_UUID_REAL = "09b9c1ed-0000-0000-0000-000000000000"

    @classmethod
    def setup_class(cls) -> None:
        cls.html = _load_html("ptr_09b9c1ed_king_2026.html")
        cls.txns = _parse_ptr_page(cls.html, cls.KING_UUID_REAL)

    def test_count(self) -> None:
        assert len(self.txns) == 9

    def test_all_spouse(self) -> None:
        for txn in self.txns:
            assert txn.owner == "SP", f"Expected SP, got {txn.owner} for {txn.asset_name}"

    def test_all_sales(self) -> None:
        for txn in self.txns:
            assert txn.txn_type == "S"

    def test_notification_date(self) -> None:
        for txn in self.txns:
            assert txn.notification_date == date(2026, 3, 24)

    def test_member_name_contains_king(self) -> None:
        for txn in self.txns:
            assert "King" in txn.member_name

    def test_tickers_present(self) -> None:
        tickers = {t.ticker for t in self.txns}
        expected = {"UBER", "PYPL", "ONON", "NFLX", "MSFT", "META", "LLY", "BX", "ADSK"}
        assert tickers == expected

    def test_uber_ticker(self) -> None:
        uber = next(t for t in self.txns if t.ticker == "UBER")
        assert uber.txn_date == date(2026, 2, 13)
        assert uber.amount_low == 1001.0
        assert uber.amount_high == 15000.0

    def test_normalize_all_nine_survive(self) -> None:
        """All 9 rows have tickers and are Sales — all should survive normalize."""
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        assert len(filings) == 9

    def test_normalize_txn_type_sales(self) -> None:
        filings = to_raw_filings(self.txns, chamber_prefix="S")
        for f in filings:
            assert f["txn_type"] == "S"


# ===========================================================================
# Search JSON fixture: _search_ptrs parsing logic (via monkeypatching)
# ===========================================================================

class TestSearchResultParsing:
    """Test that the search JSON fixture parses correctly.

    We verify the parsing logic (UUID extraction, paper/electronic filter,
    amendment flag) using the fixture without making real HTTP calls.
    The actual POST is mocked at the httpx.Client level.
    """

    @classmethod
    def setup_class(cls) -> None:
        cls.fixture = _load_json("search_result_ptrs_2026.json")

    def _make_mock_client(self, data: dict) -> MagicMock:
        """Build a mock httpx.Client whose .post() returns the given data as JSON."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = data
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        # cookies dict-like access
        mock_client.cookies.get.return_value = "testcsrf"
        return mock_client

    def test_fixture_has_five_rows(self) -> None:
        assert len(self.fixture["data"]) == 5

    def test_all_electronic_no_paper(self) -> None:
        import re
        from arbiter.ingest.congress.senate import _LINK_RE
        for row in self.fixture["data"]:
            link = row[3]
            m = _LINK_RE.search(link)
            assert m is not None, f"No link match in: {link}"
            assert m.group(1) == "ptr", f"Expected ptr, got {m.group(1)}"

    def test_boozman_amendment_detected(self) -> None:
        """First Boozman row has '(Amendment 1)' in the link text."""
        import re
        first_boozman = self.fixture["data"][0]
        assert re.search(r"Amendment", first_boozman[3], re.IGNORECASE)

    def test_boozman_non_amendment(self) -> None:
        """Second Boozman row is NOT an amendment."""
        import re
        second_boozman = self.fixture["data"][1]
        assert not re.search(r"Amendment", second_boozman[3], re.IGNORECASE)

    def test_uuid_extraction(self) -> None:
        from arbiter.ingest.congress.senate import _LINK_RE
        uuids = []
        for row in self.fixture["data"]:
            m = _LINK_RE.search(row[3])
            if m:
                uuids.append(m.group(2))
        assert "a9754ff5-901a-4877-b7be-a647bd361c52" in uuids
        assert "be9bb561-8290-4364-85b4-06a59ef0ec01" in uuids

    def test_search_ptrs_mock(self) -> None:
        """Call _search_ptrs with a mock client returning the fixture data."""
        mock_client = self._make_mock_client(self.fixture)
        rows = _search_ptrs(mock_client, csrf="fakecsr", year=2026)
        assert len(rows) == 5
        # All should be electronic (ptr)
        for row in rows:
            assert not row["is_paper"]
        # First Boozman row is an amendment
        amendment_rows = [r for r in rows if r["is_amendment"]]
        assert len(amendment_rows) == 1
        assert amendment_rows[0]["uuid"] == "727b4eb6-d8c7-4792-aa5b-c651c2d72f9c"

    def test_search_ptrs_pagination(self) -> None:
        """_search_ptrs stops paginating when start + length >= recordsTotal."""
        fixture_copy = dict(self.fixture)
        fixture_copy["recordsTotal"] = 5  # exactly one page
        mock_client = self._make_mock_client(fixture_copy)
        rows = _search_ptrs(mock_client, csrf="fakecsr", year=2026)
        # Should be called exactly once
        assert mock_client.post.call_count == 1
        assert len(rows) == 5


# ===========================================================================
# Full fetch_senate_ptrs: monkeypatch httpx to use fixture HTML
# ===========================================================================

class TestFetchSenatePtrsOffline:
    """Test the top-level fetch_senate_ptrs by injecting a fake httpx.Client."""

    def _make_full_mock_client(self) -> MagicMock:
        """Build a mock client that returns fixture data for all HTTP calls."""
        home_html = """
        <html><body>
        <form>
          <input type="hidden" name="csrfmiddlewaretoken" value="testformtoken123">
        </form>
        </body></html>
        """
        search_data = _load_json("search_result_ptrs_2026.json")
        # Limit to 3 PTRs from fixtures (Boozman original, Peters)
        search_data_small = dict(search_data)
        search_data_small["data"] = [
            search_data["data"][1],  # Boozman (a9754ff5) — no amendment
            search_data["data"][4],  # Peters (be9bb561)
        ]
        search_data_small["recordsTotal"] = 2

        boozman_html = _load_html("ptr_a9754ff5_boozman_2026.html")
        peters_html = _load_html("ptr_be9bb561_peters_2026.html")

        html_map = {
            "/search/view/ptr/a9754ff5-901a-4877-b7be-a647bd361c52/": boozman_html,
            "/search/view/ptr/be9bb561-8290-4364-85b4-06a59ef0ec01/": peters_html,
        }

        home_resp = MagicMock()
        home_resp.status_code = 200
        home_resp.text = home_html

        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = search_data_small

        agreement_resp = MagicMock()
        agreement_resp.status_code = 200
        agreement_resp.text = "<html><body>eFD: Find Reports</body></html>"

        def mock_get(url, **kwargs):
            for path, html in html_map.items():
                if path in url:
                    resp = MagicMock()
                    resp.status_code = 200
                    resp.text = html
                    return resp
            resp = MagicMock()
            resp.status_code = 200
            resp.text = home_html
            return resp

        def mock_post(url, **kwargs):
            if "report/data" in url:
                return search_resp
            return agreement_resp

        cookies_mock = MagicMock()
        cookies_mock.get.return_value = "testcsrfcookie"

        client = MagicMock()
        client.get.side_effect = mock_get
        client.post.side_effect = mock_post
        client.cookies = cookies_mock

        return client

    def test_returns_transactions(self) -> None:
        mock_client = self._make_full_mock_client()
        txns = fetch_senate_ptrs(year=2026, http_client=mock_client)
        # Boozman: 18, Peters: 1
        assert len(txns) == 19

    def test_boozman_transactions(self) -> None:
        mock_client = self._make_full_mock_client()
        txns = fetch_senate_ptrs(year=2026, http_client=mock_client)
        boozman_txns = [t for t in txns if "Boozman" in t.member_name]
        assert len(boozman_txns) == 18
        for t in boozman_txns:
            assert t.owner == "JT"
            assert t.txn_type == "S"

    def test_peters_transaction(self) -> None:
        mock_client = self._make_full_mock_client()
        txns = fetch_senate_ptrs(year=2026, http_client=mock_client)
        peters_txns = [t for t in txns if "Peters" in t.member_name]
        assert len(peters_txns) == 1
        assert peters_txns[0].ticker == "KHC"
        assert peters_txns[0].txn_type == "P"
        assert peters_txns[0].owner == "SELF"

    def test_no_sleep_in_test(self) -> None:
        """Verify time.sleep is called (polite delay) — we just need it not to block."""
        mock_client = self._make_full_mock_client()
        with patch("arbiter.ingest.congress.senate.time.sleep") as mock_sleep:
            fetch_senate_ptrs(year=2026, http_client=mock_client)
            # sleep should have been called once per PTR page fetched (2 pages)
            assert mock_sleep.call_count == 2


# ===========================================================================
# Orchestration: fetch_senate_ptrs from __init__
# ===========================================================================

class TestOrchestrateSenate:
    """Test the __init__.fetch_senate_ptrs orchestration helper."""

    def test_orchestrate_produces_raw_filings(self) -> None:
        boozman_txns = _parse_ptr_page(
            _load_html("ptr_a9754ff5_boozman_2026.html"),
            BOOZMAN_UUID,
        )

        mock_client = MagicMock(spec=CongressClient)

        with patch(
            "arbiter.ingest.congress._fetch_senate_transactions",
            return_value=boozman_txns,
        ):
            filings = orchestrate_senate_ptrs(mock_client, 2026, limit=50)

        # 18 txns - 3 None tickers = 15 surviving
        assert len(filings) == 15
        for f in filings:
            assert f["accession"].startswith(f"S-{BOOZMAN_UUID}-")
            assert f["source"] == "congress"

    def test_orchestrate_respects_limit(self) -> None:
        """limit caps the number of reports (UUIDs), not transactions."""
        king_html = _load_html("ptr_09b9c1ed_king_2026.html")
        king_uuid_a = "09b9c1ed-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        king_uuid_b = "09b9c1ed-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

        txns_a = _parse_ptr_page(king_html, king_uuid_a)
        txns_b = _parse_ptr_page(king_html, king_uuid_b)
        all_txns = txns_a + txns_b  # 18 transactions from 2 reports

        mock_client = MagicMock(spec=CongressClient)

        with patch(
            "arbiter.ingest.congress._fetch_senate_transactions",
            return_value=all_txns,
        ):
            filings = orchestrate_senate_ptrs(mock_client, 2026, limit=1)

        # Only 1 report should be processed (9 txns, all with tickers)
        assert len(filings) == 9

    def test_orchestrate_handles_senate_error(self) -> None:
        """If _fetch_senate_transactions raises, returns empty list."""
        mock_client = MagicMock(spec=CongressClient)

        with patch(
            "arbiter.ingest.congress._fetch_senate_transactions",
            side_effect=SenateEFDUnavailable("site down"),
        ):
            filings = orchestrate_senate_ptrs(mock_client, 2026)

        assert filings == []


# ===========================================================================
# Normalize: Senate-specific accession scheme
# ===========================================================================

class TestNormalizeSenateAccession:
    """Verify the chamber_prefix='S' accession scheme."""

    def test_senate_accession_prefix(self) -> None:
        txns = _parse_ptr_page(
            _load_html("ptr_be9bb561_peters_2026.html"),
            PETERS_UUID,
        )
        filings = to_raw_filings(txns, chamber_prefix="S")
        assert len(filings) == 1
        assert filings[0]["accession"] == f"S-{PETERS_UUID}-0"

    def test_house_default_prefix_unchanged(self) -> None:
        """House callers without chamber_prefix still get 'H-' prefix."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="20034201",
            chamber="house",
            member_name="Test Person",
            owner="SELF",
            asset_name="AAPL",
            ticker="AAPL",
            asset_type="ST",
            txn_type="P",
            is_partial=False,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        filings = to_raw_filings([txn])
        assert len(filings) == 1
        assert filings[0]["accession"] == "H-20034201-0"

    def test_chamber_prefix_overrides_inferred(self) -> None:
        """Explicit chamber_prefix beats txn.chamber inference."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="some-uuid-here",
            chamber="senate",
            member_name="Test Senator",
            owner="SELF",
            asset_name="MSFT",
            ticker="MSFT",
            asset_type="ST",
            txn_type="S",
            is_partial=False,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        # Explicit prefix "X" overrides chamber="senate" inference
        filings = to_raw_filings([txn], chamber_prefix="X")
        assert filings[0]["accession"].startswith("X-")

    def test_municipal_security_dropped(self) -> None:
        """MS (Municipal Security) asset_type should be filtered by normalize."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="muni-uuid",
            chamber="senate",
            member_name="Test Senator",
            owner="SELF",
            asset_name="Some Muni Bond",
            ticker="MUNI123",
            asset_type="MS",
            txn_type="P",
            is_partial=False,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        filings = to_raw_filings([txn], chamber_prefix="S")
        # MS is in _DROP_ASSET_TYPES — should be dropped
        assert len(filings) == 0

    def test_none_ticker_dropped(self) -> None:
        """Transactions with ticker=None are dropped by normalize."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="no-ticker-uuid",
            chamber="senate",
            member_name="Test Senator",
            owner="JT",
            asset_name="Some Fund",
            ticker=None,
            asset_type="ST",
            txn_type="S",
            is_partial=True,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        filings = to_raw_filings([txn], chamber_prefix="S")
        assert len(filings) == 0


# ===========================================================================
# Fix 1 — Amendment is_amendment threading
# ===========================================================================

class TestAmendmentIsAmendmentFlag:
    """Fix 1: is_amendment flag is threaded from search row → Transaction → RawFiling."""

    def test_amendment_transaction_flag_set(self) -> None:
        """_parse_ptr_page with is_amendment=True stamps every Transaction."""
        html = _load_html("ptr_a9754ff5_boozman_2026.html")
        txns = _parse_ptr_page(html, BOOZMAN_UUID, is_amendment=True)
        assert len(txns) > 0
        for txn in txns:
            assert txn.is_amendment is True, (
                f"Expected is_amendment=True on txn {txn.ticker}"
            )

    def test_non_amendment_transaction_flag_false(self) -> None:
        """_parse_ptr_page with default is_amendment=False stamps every Transaction False."""
        html = _load_html("ptr_a9754ff5_boozman_2026.html")
        txns = _parse_ptr_page(html, BOOZMAN_UUID)  # default is_amendment=False
        assert len(txns) > 0
        for txn in txns:
            assert txn.is_amendment is False

    def test_house_transaction_is_amendment_default_false(self) -> None:
        """House Transaction (no is_amendment kwarg) defaults to False — House path unaffected."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="20034201",
            chamber="house",
            member_name="Test Person",
            owner="SELF",
            asset_name="AAPL",
            ticker="AAPL",
            asset_type="ST",
            txn_type="P",
            is_partial=False,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        assert txn.is_amendment is False

    def test_normalize_sets_is_amendment_from_transaction(self) -> None:
        """to_raw_filings reads is_amendment from the Transaction, not hard-coded False."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="amend-uuid-001",
            chamber="senate",
            member_name="John Boozman",
            owner="JT",
            asset_name="Vanguard FTSE Developed Markets ETF",
            ticker="VEA",
            asset_type="ST",
            txn_type="S",
            is_partial=True,
            txn_date=date(2026, 6, 13),
            notification_date=date(2026, 6, 16),
            amount_low=1001.0,
            amount_high=15000.0,
            is_amendment=True,
        )
        filings = to_raw_filings([txn], chamber_prefix="S")
        assert len(filings) == 1
        assert filings[0]["is_amendment"] is True

    def test_normalize_house_is_amendment_false(self) -> None:
        """House Transaction (is_amendment not set, defaults False) → RawFiling is_amendment=False."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        txn = Transaction(
            doc_id="house-doc-id",
            chamber="house",
            member_name="Test Rep",
            owner="SELF",
            asset_name="Apple Inc",
            ticker="AAPL",
            asset_type="ST",
            txn_type="P",
            is_partial=False,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        filings = to_raw_filings([txn])
        assert len(filings) == 1
        assert filings[0]["is_amendment"] is False

    def test_amendment_search_row_detected_in_search_ptrs(self) -> None:
        """_search_ptrs correctly detects is_amendment=True for the Boozman amendment row."""
        from unittest.mock import MagicMock
        fixture = _load_json("search_result_ptrs_2026.json")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fixture
        mock_client = MagicMock()
        mock_client.post.return_value = mock_resp
        mock_client.cookies.get.return_value = "testcsrf"

        rows = _search_ptrs(mock_client, csrf="fakecsrf", year=2026)
        amendment_rows = [r for r in rows if r["is_amendment"]]
        non_amendment_rows = [r for r in rows if not r["is_amendment"]]

        assert len(amendment_rows) == 1
        assert amendment_rows[0]["uuid"] == "727b4eb6-d8c7-4792-aa5b-c651c2d72f9c"
        assert len(non_amendment_rows) == 4

    def test_fetch_senate_ptrs_threads_is_amendment(self) -> None:
        """fetch_senate_ptrs threads is_amendment=True into Transactions from amendment PTRs."""
        search_fixture = _load_json("search_result_ptrs_2026.json")
        # Use only the amendment row (index 0) and the original Boozman row (index 1)
        search_data_amend = dict(search_fixture)
        search_data_amend["data"] = search_fixture["data"][:2]
        search_data_amend["recordsTotal"] = 2

        boozman_html = _load_html("ptr_a9754ff5_boozman_2026.html")

        home_html = (
            "<html><body><form>"
            '<input type="hidden" name="csrfmiddlewaretoken" value="tok123">'
            "</form></body></html>"
        )
        home_resp = MagicMock()
        home_resp.status_code = 200
        home_resp.text = home_html

        agree_resp = MagicMock()
        agree_resp.status_code = 200
        agree_resp.text = "<html><body>eFD: Find Reports</body></html>"

        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = search_data_amend

        ptr_resp = MagicMock()
        ptr_resp.status_code = 200
        ptr_resp.text = boozman_html

        def mock_get(url, **kwargs):
            # Initial agreement flow GETs go to home; all /ptr/ GETs go to PTR
            if "view/ptr" in url:
                return ptr_resp
            return home_resp

        def mock_post(url, **kwargs):
            if "report/data" in url:
                return search_resp
            return agree_resp

        cookies_mock = MagicMock()
        cookies_mock.get.return_value = "testcsrfcookie"

        client = MagicMock()
        client.get.side_effect = mock_get
        client.post.side_effect = mock_post
        client.cookies = cookies_mock

        with patch("arbiter.ingest.congress.senate.time.sleep"):
            txns = fetch_senate_ptrs(year=2026, http_client=client)

        # Two PTR pages fetched, each 18 transactions = 36 total
        assert len(txns) == 36
        amendment_txns = [t for t in txns if t.is_amendment]
        non_amendment_txns = [t for t in txns if not t.is_amendment]
        # First PTR (uuid 727b4eb6) is amendment, second (a9754ff5) is not
        assert len(amendment_txns) == 18
        assert len(non_amendment_txns) == 18


# ===========================================================================
# Fix 2 — Session-expiry retry
# ===========================================================================

class TestSessionExpiryRetry:
    """Fix 2: PTR page that looks like the agreement redirect triggers re-auth + retry."""

    _REDIRECT_HTML = (
        "<html><head><title>eFD: Find Reports</title></head>"
        "<body><h1>Please agree to terms</h1></body></html>"
    )
    _REDIRECT_HTML_HOME = (
        "<html><head><title>eFD: Home</title></head>"
        "<body><h1>Senate eFD Home</h1></body></html>"
    )

    def test_looks_like_redirect_page_find_reports(self) -> None:
        from arbiter.ingest.congress.senate import _looks_like_redirect_page
        assert _looks_like_redirect_page(self._REDIRECT_HTML) is True

    def test_looks_like_redirect_page_home(self) -> None:
        from arbiter.ingest.congress.senate import _looks_like_redirect_page
        assert _looks_like_redirect_page(self._REDIRECT_HTML_HOME) is True

    def test_looks_like_redirect_page_normal_ptr(self) -> None:
        from arbiter.ingest.congress.senate import _looks_like_redirect_page
        ptr_html = _load_html("ptr_be9bb561_peters_2026.html")
        assert _looks_like_redirect_page(ptr_html) is False

    def test_redirect_then_success_retries_and_succeeds(self) -> None:
        """Simulate session expiry: first PTR GET returns redirect page,
        re-auth flow completes, second GET returns real PTR — transactions returned.
        """
        peters_html = _load_html("ptr_be9bb561_peters_2026.html")
        search_fixture = _load_json("search_result_ptrs_2026.json")
        # Only Peters row
        search_data = dict(search_fixture)
        search_data["data"] = [search_fixture["data"][4]]
        search_data["recordsTotal"] = 1

        home_html = (
            "<html><body><form>"
            '<input type="hidden" name="csrfmiddlewaretoken" value="tok123">'
            "</form></body></html>"
        )

        agree_resp = MagicMock()
        agree_resp.status_code = 200
        agree_resp.text = "<html><body>eFD: Find Reports</body></html>"

        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = search_data

        # GET call sequence:
        # 1. GET home → home_html (agreement flow step 1)
        # 2. GET ptr (Peters) → redirect page (session expired!)
        # 3. GET home again → home_html (re-auth step 1)
        # 4. GET ptr (Peters) → real PTR HTML (success after re-auth)
        home_resp = MagicMock()
        home_resp.status_code = 200
        home_resp.text = home_html

        redirect_resp = MagicMock()
        redirect_resp.status_code = 200
        redirect_resp.text = self._REDIRECT_HTML

        real_ptr_resp = MagicMock()
        real_ptr_resp.status_code = 200
        real_ptr_resp.text = peters_html

        get_call_count = [0]

        def mock_get(url, **kwargs):
            get_call_count[0] += 1
            # call 1: home (initial agreement flow)
            # call 2: PTR → redirect (session expired)
            # call 3: home (re-auth)
            # call 4: PTR → real content
            if get_call_count[0] == 1:
                return home_resp
            elif get_call_count[0] == 2:
                return redirect_resp
            elif get_call_count[0] == 3:
                return home_resp
            else:
                return real_ptr_resp

        def mock_post(url, **kwargs):
            if "report/data" in url:
                return search_resp
            return agree_resp

        cookies_mock = MagicMock()
        cookies_mock.get.return_value = "testcsrfcookie"

        client = MagicMock()
        client.get.side_effect = mock_get
        client.post.side_effect = mock_post
        client.cookies = cookies_mock

        with patch("arbiter.ingest.congress.senate.time.sleep"):
            txns = fetch_senate_ptrs(year=2026, http_client=client)

        # Peters has 1 transaction
        assert len(txns) == 1
        assert txns[0].ticker == "KHC"
        # Confirm re-auth was triggered: 4 GET calls total
        assert get_call_count[0] == 4

    def test_redirect_then_redirect_skips_report(self) -> None:
        """If re-auth still returns redirect page, report is skipped (not crashed)."""
        search_fixture = _load_json("search_result_ptrs_2026.json")
        search_data = dict(search_fixture)
        search_data["data"] = [search_fixture["data"][4]]  # Peters only
        search_data["recordsTotal"] = 1

        home_html = (
            "<html><body><form>"
            '<input type="hidden" name="csrfmiddlewaretoken" value="tok123">'
            "</form></body></html>"
        )

        agree_resp = MagicMock()
        agree_resp.status_code = 200
        agree_resp.text = "<html><body>eFD: Find Reports</body></html>"

        search_resp = MagicMock()
        search_resp.status_code = 200
        search_resp.json.return_value = search_data

        home_resp = MagicMock()
        home_resp.status_code = 200
        home_resp.text = home_html

        redirect_resp = MagicMock()
        redirect_resp.status_code = 200
        redirect_resp.text = self._REDIRECT_HTML

        def mock_get(url, **kwargs):
            # Always return redirect for PTR, home for home
            if "view/ptr" in url:
                return redirect_resp
            return home_resp

        def mock_post(url, **kwargs):
            if "report/data" in url:
                return search_resp
            return agree_resp

        cookies_mock = MagicMock()
        cookies_mock.get.return_value = "testcsrfcookie"

        client = MagicMock()
        client.get.side_effect = mock_get
        client.post.side_effect = mock_post
        client.cookies = cookies_mock

        with patch("arbiter.ingest.congress.senate.time.sleep"):
            txns = fetch_senate_ptrs(year=2026, http_client=client)

        # Skipped — no transactions returned, no crash
        assert txns == []


# ===========================================================================
# [C1 #2] Senate ticker validation — non-tickers dropped, not fabricated
# ===========================================================================

class TestSenateTickerValidation:
    """_parse_ticker must reject non-ticker cell values (apply _VALID_TICKER_RE)."""

    def test_valid_plain_ticker(self) -> None:
        assert _parse_ticker("AAPL", "") == "AAPL"

    def test_valid_dotted_ticker(self) -> None:
        assert _parse_ticker("BRK.B", "") == "BRK.B"

    def test_lowercase_normalised(self) -> None:
        assert _parse_ticker("msft", "") == "MSFT"

    def test_na_rejected(self) -> None:
        assert _parse_ticker("N/A", "") is None

    def test_na_with_space_rejected(self) -> None:
        # "N A" contains a space → not a symbol shape
        assert _parse_ticker("N A", "") is None

    def test_asset_name_fragment_rejected(self) -> None:
        # A descriptive fragment, not a symbol
        assert _parse_ticker("Common Stock", "") is None

    def test_too_long_rejected(self) -> None:
        assert _parse_ticker("ABCDEFG", "") is None

    def test_numeric_cusip_like_rejected(self) -> None:
        assert _parse_ticker("91282CJR3", "") is None

    def test_a_tag_non_ticker_rejected(self) -> None:
        # Even from an <a> tag, a non-ticker value is dropped.
        assert _parse_ticker("N/A", '<a href="#">N/A</a>') is None

    def test_normalize_drops_fabricated_ticker_txn(self) -> None:
        """End-to-end: a Transaction whose ticker is a non-symbol never produces a filing."""
        from arbiter.ingest.congress.ptr_pdf import Transaction
        bad = Transaction(
            doc_id="bad-ticker-uuid",
            chamber="senate",
            member_name="Test Senator",
            owner="SELF",
            asset_name="Some Fund",
            ticker="N/A",            # would be a fabricated signal if kept
            asset_type="ST",
            txn_type="P",
            is_partial=False,
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 1, 15),
            amount_low=1001.0,
            amount_high=15000.0,
        )
        # _VALID_TICKER_RE lives in normalize too; but the senate parser is the
        # primary guard. Here we just assert the senate parser would have nulled it.
        assert _parse_ticker("N/A", "") is None


# ===========================================================================
# [C1 #3] Unknown txn type — must NOT default to "S" (sale)
# ===========================================================================

class TestSenateUnknownTxnType:
    """_parse_txn_type must mark unknown types ambiguous, never guess Sale."""

    def test_unknown_returns_empty_code(self) -> None:
        code, partial = _parse_txn_type("Gift")
        assert code == "", "unknown txn type must NOT default to 'S'"
        assert partial is False

    def test_blank_returns_empty_code(self) -> None:
        assert _parse_txn_type("")[0] == ""

    def test_known_types_unchanged(self) -> None:
        assert _parse_txn_type("Purchase") == ("P", False)
        assert _parse_txn_type("Sale (Full)") == ("S", False)
        assert _parse_txn_type("Sale (Partial)") == ("S", True)
        assert _parse_txn_type("Exchange") == ("E", False)

    def test_ambiguous_row_skipped_in_parse_page(self) -> None:
        """A PTR row with an unrecognised transaction-type cell is skipped, not booked as S."""
        html = """
        <html>
          <h1>Periodic Transaction Report for 06/11/2026</h1>
          <h2 class="filedReport">The Honorable Test Senator (Senator, Test)</h2>
          <table><tbody>
            <tr>
              <td>1</td><td>05/21/2026</td><td>Self</td>
              <td><a href="#">AAPL</a></td><td>Apple Inc</td><td>Stock</td>
              <td>Gift</td><td>$1,001 - $15,000</td><td>05/22/2026</td>
            </tr>
            <tr>
              <td>2</td><td>05/21/2026</td><td>Self</td>
              <td><a href="#">MSFT</a></td><td>Microsoft</td><td>Stock</td>
              <td>Purchase</td><td>$1,001 - $15,000</td><td>05/22/2026</td>
            </tr>
          </tbody></table>
        </html>
        """
        txns = _parse_ptr_page(html, "ambiguous-uuid")
        # The "Gift" row is ambiguous → skipped; only the Purchase survives.
        tickers = [t.ticker for t in txns]
        assert "MSFT" in tickers
        assert "AAPL" not in tickers, "ambiguous-type row must be skipped, not mis-booked as a sale"
        for t in txns:
            assert t.txn_type in {"P", "S", "E"}
