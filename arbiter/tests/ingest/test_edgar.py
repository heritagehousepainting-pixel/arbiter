"""Tests for the EDGAR Form 4 ingestion adapter — Lane 5a.

All tests are FULLY OFFLINE.  No network calls are made.
Fixture XML is embedded as module-level constants.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from arbiter.ingest.edgar.parser import parse_form4
from arbiter.ingest.edgar.normalize import normalize, RawFiling
from arbiter.ingest.edgar.client import EdgarClient, EdgarError


# ---------------------------------------------------------------------------
# XML Fixtures
# ---------------------------------------------------------------------------

# Standard open-market BUY (P) — no 10b5-1 plan, not an amendment
FORM4_BUY_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-03-15</periodOfReport>
  <filingDate>2026-03-17</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001234567</rptOwnerCik>
      <rptOwnerName>Jane Q. Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>5000</value></transactionShares>
        <transactionPricePerShare><value>42.75</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>25000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Open-market SELL (S) — no 10b5-1, not amendment
FORM4_SELL_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-03-20</periodOfReport>
  <filingDate>2026-03-21</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0009876543</rptOwnerCik>
      <rptOwnerName>Bob Executive</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-03-20</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>2000</value></transactionShares>
        <transactionPricePerShare><value>88.50</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# 10b5-1 plan trade: <planName> element present
FORM4_10B51_PLANNAME_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-04-01</periodOfReport>
  <filingDate>2026-04-02</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0001111111</rptOwnerCik>
      <rptOwnerName>Plan Trader</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-04-01</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
        <equitySwapInvolved>0</equitySwapInvolved>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>55.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <planName>10b5-1 Trading Plan dated 2025-01-01</planName>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# 10b5-1 plan trade: footnote contains "10b5-1"
FORM4_10B51_FOOTNOTE_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-04-10</periodOfReport>
  <filingDate>2026-04-11</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0002222222</rptOwnerCik>
      <rptOwnerName>Footnote Trader</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <footnotes>
    <footnote id="F1">Sale pursuant to a Rule 10b5-1 trading plan adopted on 2025-06-01.</footnote>
  </footnotes>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-04-10</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>3000</value></transactionShares>
        <transactionPricePerShare><value>100.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Amendment (Form 4/A) — open-market buy, not 10b5-1
FORM4_AMENDMENT_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4/A</documentType>
  <periodOfReport>2026-05-01</periodOfReport>
  <filingDate>2026-05-05</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0003333333</rptOwnerCik>
      <rptOwnerName>Amending Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-05-01</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>75.25</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Option exercise (code A) — should be excluded
FORM4_OPTION_EXERCISE_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-06-01</periodOfReport>
  <filingDate>2026-06-02</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0004444444</rptOwnerCik>
      <rptOwnerName>Options Guy</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-01</value></transactionDate>
      <transactionCoding>
        <transactionCode>A</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>10.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Gift (code G) — should be excluded
FORM4_GIFT_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-06-10</periodOfReport>
  <filingDate>2026-06-11</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0005555555</rptOwnerCik>
      <rptOwnerName>Generous Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-10</value></transactionDate>
      <transactionCoding>
        <transactionCode>G</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>200</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Multiple transactions in one filing — buy + sell + option (only buy & sell should survive)
FORM4_MULTI_TXN_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-06-15</periodOfReport>
  <filingDate>2026-06-16</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0006666666</rptOwnerCik>
      <rptOwnerName>Multi Transaction Exec</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>1000</value></transactionShares>
        <transactionPricePerShare><value>50.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>51.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-06-15</value></transactionDate>
      <transactionCoding>
        <transactionCode>M</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>300</value></transactionShares>
        <transactionPricePerShare><value>20.00</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


# ---------------------------------------------------------------------------
# Helper constants
# ---------------------------------------------------------------------------

_TICKER = "ACME"
_ACCESSION = "0001234567-26-000001"


# ===========================================================================
# Parser tests
# ===========================================================================

class TestParseForm4Buy:
    """parse_form4 on a standard open-market buy."""

    def setup_method(self):
        self.rows = parse_form4(FORM4_BUY_XML, ticker=_TICKER, accession=_ACCESSION)

    def test_one_row(self):
        assert len(self.rows) == 1

    def test_ticker(self):
        assert self.rows[0]["ticker"] == _TICKER

    def test_person_id(self):
        assert self.rows[0]["person_id"] == "0001234567"

    def test_person_name(self):
        assert self.rows[0]["person_name"] == "Jane Q. Insider"

    def test_filing_ts_is_tz_aware(self):
        ts_str = self.rows[0]["filing_ts"]
        dt = datetime.fromisoformat(ts_str)
        assert dt.tzinfo is not None, "filing_ts must be tz-aware"

    def test_filing_ts_utc(self):
        ts_str = self.rows[0]["filing_ts"]
        dt = datetime.fromisoformat(ts_str)
        assert dt.utcoffset().total_seconds() == 0

    def test_filing_ts_date(self):
        ts_str = self.rows[0]["filing_ts"]
        dt = datetime.fromisoformat(ts_str)
        assert dt.date().isoformat() == "2026-03-15"

    def test_transaction_code_buy(self):
        assert self.rows[0]["transaction_code"] == "P"

    def test_shares(self):
        assert self.rows[0]["shares"] == 5000.0

    def test_price(self):
        assert self.rows[0]["price"] == pytest.approx(42.75)

    def test_amount_low(self):
        assert self.rows[0]["amount_low"] == pytest.approx(5000 * 42.75)

    def test_amount_high(self):
        assert self.rows[0]["amount_high"] == pytest.approx(5000 * 42.75)

    def test_not_10b5_1(self):
        assert self.rows[0]["is_10b5_1"] is False

    def test_not_amendment(self):
        assert self.rows[0]["is_amendment"] is False

    def test_accession(self):
        assert self.rows[0]["accession"] == _ACCESSION


class TestParseForm4Sell:
    """parse_form4 on a standard open-market sell."""

    def setup_method(self):
        self.rows = parse_form4(FORM4_SELL_XML, ticker=_TICKER, accession=_ACCESSION)

    def test_one_row(self):
        assert len(self.rows) == 1

    def test_transaction_code_sell(self):
        assert self.rows[0]["transaction_code"] == "S"

    def test_person_name(self):
        assert self.rows[0]["person_name"] == "Bob Executive"


class TestParseForm4Amendment:
    """parse_form4 correctly sets is_amendment for 4/A filings."""

    def setup_method(self):
        self.rows = parse_form4(FORM4_AMENDMENT_XML, ticker=_TICKER, accession=_ACCESSION)

    def test_one_row(self):
        assert len(self.rows) == 1

    def test_is_amendment_true(self):
        assert self.rows[0]["is_amendment"] is True

    def test_transaction_code_still_p(self):
        assert self.rows[0]["transaction_code"] == "P"


class TestParseForm410b51PlanName:
    """parse_form4 detects 10b5-1 via <planName> element."""

    def setup_method(self):
        self.rows = parse_form4(FORM4_10B51_PLANNAME_XML, ticker=_TICKER, accession=_ACCESSION)

    def test_one_row(self):
        assert len(self.rows) == 1

    def test_is_10b5_1_true(self):
        assert self.rows[0]["is_10b5_1"] is True


class TestParseForm410b51Footnote:
    """parse_form4 detects 10b5-1 via footnote text."""

    def setup_method(self):
        self.rows = parse_form4(FORM4_10B51_FOOTNOTE_XML, ticker=_TICKER, accession=_ACCESSION)

    def test_one_row(self):
        assert len(self.rows) == 1

    def test_is_10b5_1_true_from_footnote(self):
        assert self.rows[0]["is_10b5_1"] is True


class TestParseForm4MultiTxn:
    """parse_form4 returns all transaction rows regardless of code."""

    def setup_method(self):
        self.rows = parse_form4(FORM4_MULTI_TXN_XML, ticker=_TICKER, accession=_ACCESSION)

    def test_three_rows_returned(self):
        # parser returns ALL transaction rows; normalizer filters
        assert len(self.rows) == 3

    def test_codes(self):
        codes = [r["transaction_code"] for r in self.rows]
        assert codes == ["P", "S", "M"]


# ===========================================================================
# Normalize tests
# ===========================================================================

class TestNormalizeBuy:
    """normalize() produces a correct RawFiling for an open-market buy."""

    def setup_method(self):
        parsed = parse_form4(FORM4_BUY_XML, ticker=_TICKER, accession=_ACCESSION)
        self.filings: list[RawFiling] = normalize(parsed)

    def test_one_filing(self):
        assert len(self.filings) == 1

    def test_source(self):
        assert self.filings[0]["source"] == "form4"

    def test_txn_type_buy(self):
        assert self.filings[0]["txn_type"] == "P"

    def test_is_10b5_1_false(self):
        assert self.filings[0]["is_10b5_1"] is False

    def test_is_amendment_false(self):
        assert self.filings[0]["is_amendment"] is False

    def test_raw_json_is_valid_json(self):
        raw = json.loads(self.filings[0]["raw_json"])
        assert raw["ticker"] == _TICKER

    def test_shares(self):
        assert self.filings[0]["shares"] == 5000.0

    def test_price(self):
        assert self.filings[0]["price"] == pytest.approx(42.75)

    def test_amount_low(self):
        assert self.filings[0]["amount_low"] == pytest.approx(5000 * 42.75)

    def test_amount_high(self):
        assert self.filings[0]["amount_high"] == pytest.approx(5000 * 42.75)

    def test_filing_ts_tz_aware(self):
        dt = datetime.fromisoformat(self.filings[0]["filing_ts"])
        assert dt.tzinfo is not None


class TestNormalizeSell:
    """normalize() produces txn_type='S' for sells."""

    def setup_method(self):
        parsed = parse_form4(FORM4_SELL_XML, ticker=_TICKER, accession=_ACCESSION)
        self.filings = normalize(parsed)

    def test_txn_type_sell(self):
        assert self.filings[0]["txn_type"] == "S"


class TestNormalize10b51PlanExcluded:
    """normalize() drops filings flagged as 10b5-1 plan trades."""

    def test_planname_excluded(self):
        parsed = parse_form4(FORM4_10B51_PLANNAME_XML, ticker=_TICKER, accession=_ACCESSION)
        result = normalize(parsed)
        assert result == [], "10b5-1 plan trades must be excluded"

    def test_footnote_excluded(self):
        parsed = parse_form4(FORM4_10B51_FOOTNOTE_XML, ticker=_TICKER, accession=_ACCESSION)
        result = normalize(parsed)
        assert result == [], "10b5-1 footnote trades must be excluded"


class TestNormalizeAmendment:
    """normalize() passes through 4/A filings with is_amendment=True."""

    def setup_method(self):
        parsed = parse_form4(FORM4_AMENDMENT_XML, ticker=_TICKER, accession=_ACCESSION)
        self.filings = normalize(parsed)

    def test_not_excluded(self):
        assert len(self.filings) == 1, "Amendment filings should NOT be excluded"

    def test_is_amendment_true(self):
        assert self.filings[0]["is_amendment"] is True

    def test_txn_type_p(self):
        assert self.filings[0]["txn_type"] == "P"

    def test_source(self):
        assert self.filings[0]["source"] == "form4"


class TestNormalizeNonOpenMarketExcluded:
    """normalize() drops option exercises and gifts (codes A, G, M, …)."""

    def test_option_exercise_excluded(self):
        parsed = parse_form4(FORM4_OPTION_EXERCISE_XML, ticker=_TICKER, accession=_ACCESSION)
        result = normalize(parsed)
        assert result == [], "Option exercises (code A) must be excluded"

    def test_gift_excluded(self):
        parsed = parse_form4(FORM4_GIFT_XML, ticker=_TICKER, accession=_ACCESSION)
        result = normalize(parsed)
        assert result == [], "Gift transactions (code G) must be excluded"


class TestNormalizeMultiTxn:
    """normalize() keeps only open-market P/S rows from a multi-transaction filing."""

    def setup_method(self):
        parsed = parse_form4(FORM4_MULTI_TXN_XML, ticker=_TICKER, accession=_ACCESSION)
        self.filings = normalize(parsed)

    def test_two_filings(self):
        assert len(self.filings) == 2, "Only P and S rows should survive"

    def test_txn_types(self):
        codes = [f["txn_type"] for f in self.filings]
        assert set(codes) == {"P", "S"}


class TestRawFilingSchema:
    """Every RawFiling must contain all required schema keys with correct types."""

    _REQUIRED_KEYS = {
        "source", "ticker", "person_id", "person_name", "filing_ts",
        "txn_type", "shares", "price", "amount_low", "amount_high",
        "is_10b5_1", "is_amendment", "accession", "raw_json",
    }

    def setup_method(self):
        parsed = parse_form4(FORM4_BUY_XML, ticker=_TICKER, accession=_ACCESSION)
        self.filing = normalize(parsed)[0]

    def test_all_required_keys_present(self):
        missing = self._REQUIRED_KEYS - set(self.filing.keys())
        assert not missing, f"Missing RawFiling keys: {missing}"

    def test_source_is_form4(self):
        assert self.filing["source"] == "form4"

    def test_txn_type_is_p_or_s(self):
        assert self.filing["txn_type"] in {"P", "S"}

    def test_shares_is_float(self):
        assert isinstance(self.filing["shares"], float)

    def test_price_is_float(self):
        assert isinstance(self.filing["price"], float)

    def test_is_10b5_1_is_bool(self):
        assert isinstance(self.filing["is_10b5_1"], bool)

    def test_is_amendment_is_bool(self):
        assert isinstance(self.filing["is_amendment"], bool)

    def test_raw_json_parseable(self):
        data = json.loads(self.filing["raw_json"])
        assert isinstance(data, dict)


# ===========================================================================
# Client tests (no network — mocked httpx)
# ===========================================================================

class TestEdgarClientNoNetwork:
    """EdgarClient uses declared User-Agent; no real HTTP requests are made."""

    def _make_client(self, mock_http: MagicMock) -> EdgarClient:
        from arbiter.config import Config
        cfg = Config(
            live_trading=False,
            executor_backend="sim",
            db_path="data/arbiter.db",
            audit_path="data/audit.jsonl",
            metrics_path="data/metrics.jsonl",
            max_position_pct=0.05,
            max_sector_pct=0.20,
            max_gross_pct=0.80,
            max_open_positions=20,
            adv_cap_pct=0.02,
            alpaca_api_key="",
            alpaca_secret_key="",
            alpaca_paper_base_url="https://paper-api.alpaca.markets",
            alpaca_data_base_url="https://data.alpaca.markets",
            alpaca_timeout=20.0,
            edgar_user_agent="ArbiterTest test@example.com",
            kill_switch_url="",
            alert_webhook_url="",
        )
        return EdgarClient(
            config=cfg,
            http_client=mock_http,
            sleep_fn=lambda _: None,  # no real sleeping in tests
        )

    def test_user_agent_propagates(self):
        """EdgarClient stores the user-agent from Config."""
        mock_http = MagicMock()
        client = self._make_client(mock_http)
        assert client._user_agent == "ArbiterTest test@example.com"

    def test_get_form4_xml_calls_http(self):
        """get_form4_xml makes exactly 2 HTTP GET calls (index + XML)."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        # First call: index page with an XML link
        mock_resp_index = MagicMock()
        mock_resp_index.status_code = 200
        mock_resp_index.text = 'href="/Archives/edgar/data/123/000123456726000001/form4.xml"'
        # Second call: the actual XML
        mock_resp_xml = MagicMock()
        mock_resp_xml.status_code = 200
        mock_resp_xml.text = FORM4_BUY_XML

        mock_http = MagicMock()
        mock_http.get.side_effect = [mock_resp_index, mock_resp_xml]

        client = self._make_client(mock_http)
        result = client.get_form4_xml("0001234567-26-000001", "0000000123")

        assert mock_http.get.call_count == 2
        assert result == FORM4_BUY_XML

    def test_missing_user_agent_raises(self):
        """Empty edgar_user_agent must raise ValueError at construction time."""
        from arbiter.config import Config
        cfg = Config(
            live_trading=False,
            executor_backend="sim",
            db_path="data/arbiter.db",
            audit_path="data/audit.jsonl",
            metrics_path="data/metrics.jsonl",
            max_position_pct=0.05,
            max_sector_pct=0.20,
            max_gross_pct=0.80,
            max_open_positions=20,
            adv_cap_pct=0.02,
            alpaca_api_key="",
            alpaca_secret_key="",
            alpaca_paper_base_url="https://paper-api.alpaca.markets",
            alpaca_data_base_url="https://data.alpaca.markets",
            alpaca_timeout=20.0,
            edgar_user_agent="",   # <-- deliberately empty
            kill_switch_url="",
            alert_webhook_url="",
        )
        with pytest.raises(ValueError, match="edgar_user_agent"):
            EdgarClient(config=cfg, http_client=MagicMock(), sleep_fn=lambda _: None)

    def test_429_triggers_retry(self):
        """HTTP 429 responses trigger back-off retries."""
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429

        mock_resp_200 = MagicMock()
        mock_resp_200.status_code = 200
        mock_resp_200.text = "ok"

        mock_http = MagicMock()
        # First call: 429; second: 200
        mock_http.get.side_effect = [mock_resp_429, mock_resp_200]

        client = self._make_client(mock_http)
        result = client._get("https://www.sec.gov/test")
        assert result == "ok"
        assert mock_http.get.call_count == 2

    def test_all_retries_fail_raises_edgar_error(self):
        """If all retries fail, EdgarError is raised."""
        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429

        mock_http = MagicMock()
        mock_http.get.return_value = mock_resp_429

        client = self._make_client(mock_http)
        with pytest.raises(EdgarError):
            client._get("https://www.sec.gov/test")


# ===========================================================================
# End-to-end integration: fixture XML → RawFiling
# ===========================================================================

class TestEndToEnd:
    """Full parse → normalize pipeline on fixture XML."""

    def test_buy_produces_rawfiling(self):
        parsed = parse_form4(FORM4_BUY_XML, ticker="ACME", accession=_ACCESSION)
        result = normalize(parsed)
        assert len(result) == 1
        f = result[0]
        assert f["source"] == "form4"
        assert f["ticker"] == "ACME"
        assert f["txn_type"] == "P"
        assert f["is_10b5_1"] is False
        assert f["is_amendment"] is False

    def test_sell_produces_rawfiling(self):
        parsed = parse_form4(FORM4_SELL_XML, ticker="ACME", accession=_ACCESSION)
        result = normalize(parsed)
        assert len(result) == 1
        assert result[0]["txn_type"] == "S"

    def test_amendment_is_flagged(self):
        parsed = parse_form4(FORM4_AMENDMENT_XML, ticker="ACME", accession=_ACCESSION)
        result = normalize(parsed)
        assert result[0]["is_amendment"] is True

    def test_10b5_1_excluded_end_to_end(self):
        parsed = parse_form4(FORM4_10B51_PLANNAME_XML, ticker="ACME", accession=_ACCESSION)
        result = normalize(parsed)
        assert result == []

    def test_non_open_market_excluded_end_to_end(self):
        parsed = parse_form4(FORM4_OPTION_EXERCISE_XML, ticker="ACME", accession=_ACCESSION)
        result = normalize(parsed)
        assert result == []


# ===========================================================================
# P0: txn_idx — multi-transaction Form 4 must produce distinct indices
# ===========================================================================

class TestTxnIdx:
    """parse_form4 must assign a distinct txn_idx to each transaction."""

    def test_single_txn_has_txn_idx_zero(self):
        rows = parse_form4(FORM4_BUY_XML, ticker=_TICKER, accession=_ACCESSION)
        assert rows[0]["txn_idx"] == 0

    def test_multi_txn_indices_are_sequential(self):
        rows = parse_form4(FORM4_MULTI_TXN_XML, ticker=_TICKER, accession=_ACCESSION)
        assert [r["txn_idx"] for r in rows] == [0, 1, 2]

    def test_normalize_preserves_txn_idx(self):
        parsed = parse_form4(FORM4_MULTI_TXN_XML, ticker=_TICKER, accession=_ACCESSION)
        filings = normalize(parsed)
        # After filtering (P and S survive, M is excluded), txn_idx 0 and 1 survive.
        assert len(filings) == 2
        assert filings[0]["txn_idx"] == 0
        assert filings[1]["txn_idx"] == 1


# ===========================================================================
# P1: Missing price → amount_low/high must be None (not 0)
# ===========================================================================

# Form 4 with no price disclosed — price element is absent
FORM4_NO_PRICE_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-07-01</periodOfReport>
  <filingDate>2026-07-02</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0007777777</rptOwnerCik>
      <rptOwnerName>No Price Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-01</value></transactionDate>
      <transactionCoding>
        <transactionCode>P</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>3000</value></transactionShares>
        <!-- no transactionPricePerShare element -->
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""

# Form 4 with price explicitly zeroed (EDGAR sentinel for "not disclosed")
FORM4_ZERO_PRICE_XML = """\
<?xml version="1.0"?>
<ownershipDocument>
  <documentType>4</documentType>
  <periodOfReport>2026-07-05</periodOfReport>
  <filingDate>2026-07-06</filingDate>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerCik>0008888888</rptOwnerCik>
      <rptOwnerName>Zero Price Insider</rptOwnerName>
    </reportingOwnerId>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <securityTitle><value>Common Stock</value></securityTitle>
      <transactionDate><value>2026-07-05</value></transactionDate>
      <transactionCoding>
        <transactionCode>S</transactionCode>
      </transactionCoding>
      <transactionAmounts>
        <transactionShares><value>500</value></transactionShares>
        <transactionPricePerShare><value>0</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>D</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


class TestMissingPrice:
    """Parser and normalizer must produce None (not 0) when price is absent."""

    def test_parser_no_price_element_gives_none(self):
        rows = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        assert rows[0]["price"] is None, "Missing price element must yield None"

    def test_parser_no_price_amount_low_is_none(self):
        rows = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        assert rows[0]["amount_low"] is None

    def test_parser_no_price_amount_high_is_none(self):
        rows = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        assert rows[0]["amount_high"] is None

    def test_parser_zero_price_treated_as_none(self):
        rows = parse_form4(FORM4_ZERO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        assert rows[0]["price"] is None, "Zero price must be treated as missing (None)"

    def test_parser_zero_price_amounts_are_none(self):
        rows = parse_form4(FORM4_ZERO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        assert rows[0]["amount_low"] is None
        assert rows[0]["amount_high"] is None

    def test_normalize_no_price_amount_low_is_none(self):
        parsed = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        filings = normalize(parsed)
        assert len(filings) == 1
        assert filings[0]["amount_low"] is None

    def test_normalize_no_price_amount_high_is_none(self):
        parsed = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        filings = normalize(parsed)
        assert filings[0]["amount_high"] is None

    def test_normalize_no_price_price_is_none(self):
        parsed = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        filings = normalize(parsed)
        assert filings[0]["price"] is None

    def test_normalize_no_price_filing_not_dropped(self):
        """A missing-price filing must NOT be silently dropped by normalize."""
        parsed = parse_form4(FORM4_NO_PRICE_XML, ticker=_TICKER, accession=_ACCESSION)
        filings = normalize(parsed)
        assert len(filings) == 1, "Missing-price filing must survive normalize()"

    def test_known_price_still_gives_float(self):
        """A filing with a known price must still produce float, not None."""
        rows = parse_form4(FORM4_BUY_XML, ticker=_TICKER, accession=_ACCESSION)
        assert isinstance(rows[0]["price"], float)
        assert rows[0]["price"] == pytest.approx(42.75)
