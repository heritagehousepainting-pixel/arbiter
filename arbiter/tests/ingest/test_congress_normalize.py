"""Tests for L4 normalize — arbiter.ingest.congress.normalize.

Verifies the to_raw_filings() function according to:
  - REBUILD_PLAN.md frozen Transaction + RawFiling contracts
  - INTERFACES.md §4.3 (Congress = ranges never midpoint; ~45-day lag → MEDIUM)
  - edgar normalize.py RawFiling key set (must match exactly for write_filing)

No network calls; all data is constructed inline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any

import pytest

from arbiter.ingest.congress.normalize import RawFiling, to_raw_filings

# ---------------------------------------------------------------------------
# We construct Transaction objects using the local dataclass defined inside
# normalize.py (fallback when ptr_pdf.py is not yet authored).  Import it
# from the same module to guarantee we're testing against the live contract.
# ---------------------------------------------------------------------------
try:
    from arbiter.ingest.congress.ptr_pdf import Transaction  # type: ignore[import]
except ModuleNotFoundError:
    from arbiter.ingest.congress.normalize import Transaction  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Helper: build a minimal valid Transaction with sensible defaults
# ---------------------------------------------------------------------------

def make_txn(**overrides: Any) -> Transaction:
    """Return a Transaction with all required fields set to valid defaults.

    Override any field by passing keyword arguments.
    """
    defaults: dict[str, Any] = {
        "doc_id": "20034201",
        "chamber": "house",
        "member_name": "Mark Alford",
        "owner": "SP",
        "asset_name": "Amazon.com Inc",
        "ticker": "AMZN",
        "asset_type": "ST",
        "txn_type": "S",
        "is_partial": False,
        "txn_date": date(2026, 1, 10),
        "notification_date": date(2026, 2, 24),
        "amount_low": 1_001.0,
        "amount_high": 15_000.0,
    }
    defaults.update(overrides)
    return Transaction(**defaults)


# ---------------------------------------------------------------------------
# RawFiling keys — authoritative list (must exactly match edgar's shape)
# ---------------------------------------------------------------------------

EXPECTED_KEYS = frozenset(
    {
        "source",
        "ticker",
        "person_id",
        "person_name",
        "filing_ts",
        "txn_type",
        "txn_idx",
        "shares",
        "price",
        "amount_low",
        "amount_high",
        "is_10b5_1",
        "is_amendment",
        "accession",
        "raw_json",
    }
)


# ---------------------------------------------------------------------------
# Core contract tests
# ---------------------------------------------------------------------------


class TestRawFilingKeys:
    """Output dict must have EXACTLY the same keys as edgar RawFiling."""

    def test_output_has_all_expected_keys(self) -> None:
        txn = make_txn()
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert set(result[0].keys()) == EXPECTED_KEYS

    def test_no_extra_keys(self) -> None:
        txn = make_txn()
        result = to_raw_filings([txn])
        extra = set(result[0].keys()) - EXPECTED_KEYS
        assert extra == set(), f"Unexpected extra keys: {extra}"

    def test_no_missing_keys(self) -> None:
        txn = make_txn()
        result = to_raw_filings([txn])
        missing = EXPECTED_KEYS - set(result[0].keys())
        assert missing == set(), f"Missing keys: {missing}"


class TestFilingTs:
    """filing_ts must be the notification_date (not txn_date), tz-aware UTC."""

    def test_filing_ts_is_notification_date(self) -> None:
        txn = make_txn(
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 2, 24),
        )
        result = to_raw_filings([txn])
        filing_ts = result[0]["filing_ts"]
        # Must be a tz-aware datetime representing notification_date at UTC midnight
        dt = datetime.fromisoformat(filing_ts)
        assert dt.tzinfo is not None, "filing_ts must be tz-aware"
        assert dt.date() == date(2026, 2, 24), (
            f"filing_ts must reflect notification_date (2026-02-24), got {dt.date()}"
        )

    def test_filing_ts_is_not_txn_date(self) -> None:
        """The 45-day lag means these are different; assert they differ."""
        txn = make_txn(
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 2, 24),
        )
        result = to_raw_filings([txn])
        dt = datetime.fromisoformat(result[0]["filing_ts"])
        assert dt.date() != date(2026, 1, 10), (
            "filing_ts must NOT be the transaction date"
        )

    def test_filing_ts_utc_midnight(self) -> None:
        txn = make_txn(
            txn_date=date(2025, 5, 1),
            notification_date=date(2025, 6, 1),
        )
        result = to_raw_filings([txn])
        dt = datetime.fromisoformat(result[0]["filing_ts"])
        assert dt.tzinfo == timezone.utc
        assert dt.hour == 0
        assert dt.minute == 0
        assert dt.second == 0

    def test_filing_ts_format_iso8601(self) -> None:
        txn = make_txn(notification_date=date(2026, 3, 15))
        result = to_raw_filings([txn])
        filing_ts = result[0]["filing_ts"]
        # Must be parseable as ISO-8601
        dt = datetime.fromisoformat(filing_ts)
        assert dt.year == 2026
        assert dt.month == 3
        assert dt.day == 15


class TestAmountRange:
    """amount_low / amount_high must be preserved verbatim — never midpoint-imputed."""

    def test_amount_range_preserved(self) -> None:
        txn = make_txn(amount_low=1_001.0, amount_high=15_000.0)
        result = to_raw_filings([txn])
        assert result[0]["amount_low"] == 1_001.0
        assert result[0]["amount_high"] == 15_000.0

    def test_no_midpoint_imputation(self) -> None:
        """The midpoint would be 8_000.5; neither field should equal that."""
        txn = make_txn(amount_low=1_001.0, amount_high=15_000.0)
        result = to_raw_filings([txn])
        midpoint = (1_001.0 + 15_000.0) / 2
        assert result[0]["amount_low"] != midpoint
        assert result[0]["amount_high"] != midpoint

    def test_large_amount_bracket(self) -> None:
        """Over $50M sentinel: amount_low == amount_high == 50_000_001.0."""
        txn = make_txn(amount_low=50_000_001.0, amount_high=50_000_001.0)
        result = to_raw_filings([txn])
        assert result[0]["amount_low"] == 50_000_001.0
        assert result[0]["amount_high"] == 50_000_001.0

    def test_equal_bounds_kept(self) -> None:
        txn = make_txn(amount_low=5_000.0, amount_high=5_000.0)
        result = to_raw_filings([txn])
        assert result[0]["amount_low"] == 5_000.0
        assert result[0]["amount_high"] == 5_000.0


class TestFilteringRules:
    """Drop conditions: ticker=None, txn_type="E", non-equity asset types, CUSIP tickers."""

    def test_ticker_none_dropped(self) -> None:
        txn = make_txn(ticker=None)
        result = to_raw_filings([txn])
        assert result == [], "Transaction with ticker=None must be dropped"

    def test_txn_type_exchange_dropped(self) -> None:
        txn = make_txn(txn_type="E")
        result = to_raw_filings([txn])
        assert result == [], "Transaction with txn_type='E' must be dropped"

    def test_txn_type_purchase_kept(self) -> None:
        txn = make_txn(txn_type="P")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["txn_type"] == "P"

    def test_txn_type_sale_kept(self) -> None:
        txn = make_txn(txn_type="S")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["txn_type"] == "S"

    def test_mixed_list_filters_correctly(self) -> None:
        """Exchange and no-ticker items dropped; P/S items kept."""
        txns = [
            make_txn(txn_type="P", ticker="AMZN"),   # keep  (input pos 0)
            make_txn(txn_type="E", ticker="NFLX"),   # drop: exchange
            make_txn(txn_type="S", ticker=None),     # drop: no ticker
            make_txn(txn_type="S", ticker="AAPL"),   # keep  (input pos 3)
        ]
        result = to_raw_filings(txns)
        assert len(result) == 2
        tickers = [r["ticker"] for r in result]
        assert tickers == ["AMZN", "AAPL"]

    def test_empty_input_returns_empty(self) -> None:
        assert to_raw_filings([]) == []

    def test_all_filtered_returns_empty(self) -> None:
        txns = [
            make_txn(txn_type="E"),
            make_txn(ticker=None),
        ]
        assert to_raw_filings(txns) == []

    # --- Asset-type drop set tests -----------------------------------------

    def test_gs_government_security_dropped(self) -> None:
        """Government Securities (GS) — e.g. Treasuries — must be dropped."""
        txn = make_txn(asset_type="GS", ticker="91282CNG2")
        result = to_raw_filings([txn])
        assert result == [], "GS (government security) must be dropped"

    def test_ps_partnership_dropped(self) -> None:
        """Partnership / LP units (PS) must be dropped."""
        txn = make_txn(asset_type="PS", ticker="KRSOX")
        result = to_raw_filings([txn])
        assert result == [], "PS (partnership) must be dropped"

    def test_hn_hedge_fund_dropped(self) -> None:
        """Hedge / private fund (HN) must be dropped."""
        txn = make_txn(asset_type="HN", ticker="ICAPITAL")
        result = to_raw_filings([txn])
        assert result == [], "HN (hedge fund) must be dropped"

    def test_cs_corporate_bond_dropped(self) -> None:
        """Corporate debt (CS) must be dropped."""
        txn = make_txn(asset_type="CS", ticker="CORP123")
        result = to_raw_filings([txn])
        assert result == [], "CS (corporate bond) must be dropped"

    def test_co_commodity_dropped(self) -> None:
        """Commodity (CO) must be dropped."""
        txn = make_txn(asset_type="CO", ticker="GOLD")
        result = to_raw_filings([txn])
        assert result == [], "CO (commodity) must be dropped"

    def test_ol_option_on_debt_dropped(self) -> None:
        """Option on debt/index (OL) must be dropped."""
        txn = make_txn(asset_type="OL", ticker="XYZOL")
        result = to_raw_filings([txn])
        assert result == [], "OL must be dropped"

    def test_rp_real_property_dropped(self) -> None:
        """Real property (RP) must be dropped."""
        txn = make_txn(asset_type="RP", ticker="REIT1")
        result = to_raw_filings([txn])
        assert result == [], "RP (real property) must be dropped"

    def test_ab_asset_backed_dropped(self) -> None:
        """Asset-backed securities (AB) must be dropped."""
        txn = make_txn(asset_type="AB", ticker="ABS123")
        result = to_raw_filings([txn])
        assert result == [], "AB (asset-backed security) must be dropped"

    def test_ct_certificate_dropped(self) -> None:
        """Certificates of deposit / trust receipts (CT) must be dropped."""
        txn = make_txn(asset_type="CT", ticker="CD123")
        result = to_raw_filings([txn])
        assert result == [], "CT (certificate) must be dropped"

    def test_st_stock_kept(self) -> None:
        """ST (stock) is the canonical equity type and must be kept."""
        txn = make_txn(asset_type="ST", ticker="AAPL")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"

    def test_ot_etf_qqq_kept(self) -> None:
        """OT (other) covers real ETFs like QQQ/DIA — must NOT be dropped."""
        txn = make_txn(asset_type="OT", ticker="QQQ")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["ticker"] == "QQQ"

    def test_ot_etf_dia_kept(self) -> None:
        """DIA is another real ETF filed under OT."""
        txn = make_txn(asset_type="OT", ticker="DIA")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["ticker"] == "DIA"

    def test_asset_type_none_falls_through_to_cusip_guard(self) -> None:
        """If asset_type is None, a CUSIP-shaped ticker should still be caught."""
        txn = make_txn(asset_type=None, ticker="91282CGH8")
        result = to_raw_filings([txn])
        assert result == [], "CUSIP-shaped ticker must be dropped even with asset_type=None"

    # --- CUSIP-shape ticker guard tests ------------------------------------

    def test_cusip_treasury_91282cng2_dropped(self) -> None:
        """91282CNG2 is a real Treasury CUSIP; must be dropped."""
        txn = make_txn(asset_type="OT", ticker="91282CNG2")
        result = to_raw_filings([txn])
        assert result == [], "Treasury CUSIP 91282CNG2 must be dropped"

    def test_cusip_91282cgh8_dropped(self) -> None:
        """91282CGH8 is a real Treasury CUSIP; must be dropped."""
        txn = make_txn(asset_type="OT", ticker="91282CGH8")
        result = to_raw_filings([txn])
        assert result == [], "Treasury CUSIP 91282CGH8 must be dropped"

    def test_cusip_9char_with_digit_dropped(self) -> None:
        """A generic 9-char alphanumeric with a digit is CUSIP-shaped."""
        txn = make_txn(asset_type="ST", ticker="ABCDE1234")
        result = to_raw_filings([txn])
        assert result == [], "9-char token with digit must be dropped"

    def test_cusip_all_letters_9char_kept(self) -> None:
        """9-char all-letter token is NOT a CUSIP (no digit); fall through."""
        # This would still not be a valid ticker (>5 chars), but the CUSIP
        # rule alone should not drop it — it would survive CUSIP check.
        # We test the CUSIP guard specifically here.
        txn = make_txn(asset_type="ST", ticker="ABCDEFGHI")
        # No digit in a 9-char all-alpha string → not CUSIP-shaped
        # (Normal ticker length guard is not part of the CUSIP filter)
        result = to_raw_filings([txn])
        assert len(result) == 1, "9-char all-letter token is not CUSIP-shaped"

    def test_digit_prefixed_long_token_dropped(self) -> None:
        """A token starting with a digit and >5 chars is dropped (e.g. '1ABCDE')."""
        txn = make_txn(asset_type="OT", ticker="1ABCDE")
        result = to_raw_filings([txn])
        assert result == [], "Digit-prefixed token >5 chars must be dropped"

    def test_digit_prefixed_short_token_kept(self) -> None:
        """A digit-prefixed token of ≤5 chars passes the CUSIP guard."""
        # e.g. '1ABC' (4 chars starting with digit) is not caught by the guard.
        txn = make_txn(asset_type="ST", ticker="1ABC")
        result = to_raw_filings([txn])
        assert len(result) == 1, "Digit-prefixed token ≤5 chars is not CUSIP-shaped"

    def test_brk_b_valid_ticker_kept(self) -> None:
        """BRK.B (dot-notation ticker) must pass through the CUSIP guard."""
        txn = make_txn(asset_type="ST", ticker="BRK.B")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["ticker"] == "BRK.B"

    def test_t_single_char_ticker_kept(self) -> None:
        """Single-letter tickers (e.g. T for AT&T) must pass all guards."""
        txn = make_txn(asset_type="ST", ticker="T")
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["ticker"] == "T"


class TestAccessionAndTxnIdx:
    """accession must use stable INPUT ENUMERATE POSITION, not post-filter counter."""

    def test_single_txn_accession_and_idx(self) -> None:
        txn = make_txn(doc_id="20034201")
        result = to_raw_filings([txn])
        # Input position 0 → txn_idx=0, accession H-20034201-0
        assert result[0]["txn_idx"] == 0
        assert result[0]["accession"] == "H-20034201-0"

    def test_sequential_txn_idx(self) -> None:
        txns = [
            make_txn(doc_id="20034201", ticker="AMZN"),
            make_txn(doc_id="20034201", ticker="AAPL"),
            make_txn(doc_id="20034201", ticker="T"),
        ]
        result = to_raw_filings(txns)
        # All kept at input positions 0, 1, 2
        assert [r["txn_idx"] for r in result] == [0, 1, 2]

    def test_accession_format_all_txns(self) -> None:
        txns = [
            make_txn(doc_id="20034201", ticker="AMZN"),
            make_txn(doc_id="20034201", ticker="AAPL"),
        ]
        result = to_raw_filings(txns)
        assert result[0]["accession"] == "H-20034201-0"
        assert result[1]["accession"] == "H-20034201-1"

    def test_txn_idx_uses_input_position_not_output_counter(self) -> None:
        """Filtered-out transactions consume their input positions.

        The first two input items are dropped (exchange + no-ticker).
        The third and fourth items are kept at INPUT positions 2 and 3.
        txn_idx must be 2 and 3 (not reset to 0 and 1).
        This is the stability guarantee: re-running with different filter rules
        does not change the accession of surviving transactions.
        """
        txns = [
            make_txn(doc_id="99999999", txn_type="E", ticker="X"),    # dropped; pos 0
            make_txn(doc_id="99999999", ticker=None),                  # dropped; pos 1
            make_txn(doc_id="99999999", ticker="AMZN", txn_type="P"), # kept; pos 2
            make_txn(doc_id="99999999", ticker="AAPL", txn_type="S"), # kept; pos 3
        ]
        result = to_raw_filings(txns)
        assert len(result) == 2
        assert result[0]["txn_idx"] == 2
        assert result[1]["txn_idx"] == 3
        assert result[0]["accession"] == "H-99999999-2"
        assert result[1]["accession"] == "H-99999999-3"

    def test_txn_idx_stable_across_filter_changes(self) -> None:
        """If we add a filter that drops input[0], input[1] stays at txn_idx=1."""
        txns = [
            make_txn(doc_id="STABLE", ticker="AMZN", txn_type="P"),  # pos 0 — kept
            make_txn(doc_id="STABLE", ticker="AAPL", txn_type="S"),  # pos 1 — kept
        ]
        result_full = to_raw_filings(txns)
        # Both kept: positions 0 and 1
        assert result_full[0]["txn_idx"] == 0
        assert result_full[1]["txn_idx"] == 1
        assert result_full[1]["accession"] == "H-STABLE-1"

        # Now simulate AMZN being dropped (e.g. exchange type)
        txns2 = [
            make_txn(doc_id="STABLE", ticker="AMZN", txn_type="E"),  # pos 0 — dropped
            make_txn(doc_id="STABLE", ticker="AAPL", txn_type="S"),  # pos 1 — kept
        ]
        result_partial = to_raw_filings(txns2)
        # AAPL is still at input position 1 → accession unchanged
        assert len(result_partial) == 1
        assert result_partial[0]["txn_idx"] == 1
        assert result_partial[0]["accession"] == "H-STABLE-1"


class TestDateSanityGuard:
    """notification_date must be ≥ txn_date and lag must not exceed 365 days."""

    def test_notification_before_txn_date_dropped(self) -> None:
        """notification_date < txn_date → corrupted dates → skip."""
        txn = make_txn(
            txn_date=date(2026, 2, 1),
            notification_date=date(2026, 1, 1),  # -31 days — impossible
        )
        result = to_raw_filings([txn])
        assert result == [], "notification_date before txn_date must be dropped"

    def test_large_negative_lag_dropped(self) -> None:
        """A -339-day lag (the live bug) must be caught."""
        txn = make_txn(
            txn_date=date(2025, 12, 31),
            notification_date=date(2025, 1, 27),  # -339 days
        )
        result = to_raw_filings([txn])
        assert result == [], "Negative lag of -339 days must be dropped"

    def test_lag_exceeds_365_days_dropped(self) -> None:
        """A disclosure lag of >365 days is implausible and must be skipped."""
        txn = make_txn(
            txn_date=date(2024, 1, 1),
            notification_date=date(2025, 1, 3),  # 368 days — too long
        )
        result = to_raw_filings([txn])
        assert result == [], "Lag > 365 days must be dropped"

    def test_lag_exactly_365_days_kept(self) -> None:
        """Lag of exactly 365 days is at the boundary — must be kept."""
        txn = make_txn(
            txn_date=date(2025, 1, 1),
            notification_date=date(2026, 1, 1),  # exactly 365 days
        )
        result = to_raw_filings([txn])
        assert len(result) == 1

    def test_lag_0_days_kept(self) -> None:
        """Same-day notification (lag=0) is allowed (unusual but valid)."""
        txn = make_txn(
            txn_date=date(2026, 3, 1),
            notification_date=date(2026, 3, 1),
        )
        result = to_raw_filings([txn])
        assert len(result) == 1

    def test_normal_45_day_lag_kept(self) -> None:
        """Normal ~45-day lag (the expected Congress disclosure window) is kept."""
        txn = make_txn(
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 2, 24),  # 45 days
        )
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["filing_ts"].startswith("2026-02-24")


class TestStaticFields:
    """source, person_id, shares, price, is_10b5_1, is_amendment must be hardcoded."""

    def test_source_is_congress(self) -> None:
        result = to_raw_filings([make_txn()])
        assert result[0]["source"] == "congress"

    def test_person_id_is_none(self) -> None:
        result = to_raw_filings([make_txn()])
        assert result[0]["person_id"] is None

    def test_shares_is_none(self) -> None:
        result = to_raw_filings([make_txn()])
        assert result[0]["shares"] is None

    def test_price_is_none(self) -> None:
        result = to_raw_filings([make_txn()])
        assert result[0]["price"] is None

    def test_is_10b5_1_false(self) -> None:
        result = to_raw_filings([make_txn()])
        assert result[0]["is_10b5_1"] is False

    def test_is_amendment_false(self) -> None:
        result = to_raw_filings([make_txn()])
        assert result[0]["is_amendment"] is False


class TestIdentityFields:
    """ticker and person_name must be carried through from Transaction."""

    def test_ticker_carried_through(self) -> None:
        result = to_raw_filings([make_txn(ticker="NFLX")])
        assert result[0]["ticker"] == "NFLX"

    def test_person_name_from_member_name(self) -> None:
        result = to_raw_filings([make_txn(member_name="Nancy Pelosi")])
        assert result[0]["person_name"] == "Nancy Pelosi"

    def test_brk_b_ticker_preserved(self) -> None:
        """BRK.B (already normalised by parser) passes through unchanged."""
        result = to_raw_filings([make_txn(ticker="BRK.B")])
        assert result[0]["ticker"] == "BRK.B"


class TestRawJson:
    """raw_json must be a valid JSON dump of the source Transaction fields."""

    def test_raw_json_is_valid_json(self) -> None:
        result = to_raw_filings([make_txn()])
        raw = json.loads(result[0]["raw_json"])
        assert isinstance(raw, dict)

    def test_raw_json_contains_ticker(self) -> None:
        txn = make_txn(ticker="FERG")
        result = to_raw_filings([txn])
        raw = json.loads(result[0]["raw_json"])
        assert raw.get("ticker") == "FERG"

    def test_raw_json_contains_txn_type(self) -> None:
        txn = make_txn(txn_type="P")
        result = to_raw_filings([txn])
        raw = json.loads(result[0]["raw_json"])
        assert raw.get("txn_type") == "P"

    def test_raw_json_contains_dates(self) -> None:
        txn = make_txn(
            txn_date=date(2026, 1, 10),
            notification_date=date(2026, 2, 24),
        )
        result = to_raw_filings([txn])
        raw = json.loads(result[0]["raw_json"])
        # Dates are serialised as strings via json default=str
        assert "2026-01-10" in str(raw.get("txn_date", ""))
        assert "2026-02-24" in str(raw.get("notification_date", ""))


class TestEdgeCases:
    """Defensive handling for bad / missing data."""

    def test_all_transactions_filtered_returns_empty_list(self) -> None:
        txns = [make_txn(txn_type="E") for _ in range(5)]
        assert to_raw_filings(txns) == []

    def test_large_batch_sequential_ids(self) -> None:
        txns = [make_txn(ticker=f"T{i}") for i in range(50)]
        result = to_raw_filings(txns)
        assert len(result) == 50
        assert result[0]["txn_idx"] == 0
        assert result[49]["txn_idx"] == 49

    def test_multiple_doc_ids_in_batch(self) -> None:
        """Transactions from different filings in one batch — txn_idx is global input pos."""
        txns = [
            make_txn(doc_id="AAA", ticker="AMZN"),  # pos 0
            make_txn(doc_id="BBB", ticker="AAPL"),  # pos 1
        ]
        result = to_raw_filings(txns)
        assert result[0]["accession"] == "H-AAA-0"
        assert result[1]["accession"] == "H-BBB-1"

    def test_partial_sale_kept(self) -> None:
        """is_partial=True does not affect inclusion — txn_type S is kept."""
        txn = make_txn(txn_type="S", is_partial=True)
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["txn_type"] == "S"

    def test_amount_low_zero_allowed(self) -> None:
        """amount_low=0 is valid (edge of a bracket)."""
        txn = make_txn(amount_low=0.0, amount_high=1_000.0)
        result = to_raw_filings([txn])
        assert len(result) == 1
        assert result[0]["amount_low"] == 0.0

    def test_equal_amounts_allowed(self) -> None:
        """Some brackets have low == high (e.g. 'Over $50M' sentinel)."""
        txn = make_txn(amount_low=50_000_001.0, amount_high=50_000_001.0)
        result = to_raw_filings([txn])
        assert len(result) == 1


class TestRealistScenario:
    """End-to-end scenario using data modelled on the real Allen / Alford PTR fixtures."""

    def test_allen_ptr_scenario(self) -> None:
        """Modelled on ptr_20033751: FERG buy, NFLX sale, SP owner (no exchange)."""
        txns = [
            make_txn(
                doc_id="20033751",
                member_name="Rick Allen",
                owner="SP",
                asset_name="Ferguson Enterprises Inc",
                ticker="FERG",
                txn_type="P",
                txn_date=date(2026, 1, 8),
                notification_date=date(2026, 2, 22),
                amount_low=1_001.0,
                amount_high=15_000.0,
            ),
            make_txn(
                doc_id="20033751",
                member_name="Rick Allen",
                owner="SP",
                asset_name="Netflix Inc",
                ticker="NFLX",
                txn_type="S",
                txn_date=date(2026, 1, 9),
                notification_date=date(2026, 2, 22),
                amount_low=15_001.0,
                amount_high=50_000.0,
            ),
        ]
        result = to_raw_filings(txns)

        assert len(result) == 2

        ferg, nflx = result

        # FERG purchase — input position 0
        assert ferg["ticker"] == "FERG"
        assert ferg["txn_type"] == "P"
        assert ferg["person_name"] == "Rick Allen"
        assert ferg["accession"] == "H-20033751-0"
        assert ferg["txn_idx"] == 0
        assert ferg["amount_low"] == 1_001.0
        assert ferg["amount_high"] == 15_000.0
        dt_ferg = datetime.fromisoformat(ferg["filing_ts"])
        assert dt_ferg.date() == date(2026, 2, 22)
        assert dt_ferg.tzinfo == timezone.utc

        # NFLX sale — input position 1
        assert nflx["ticker"] == "NFLX"
        assert nflx["txn_type"] == "S"
        assert nflx["accession"] == "H-20033751-1"
        assert nflx["txn_idx"] == 1
        assert nflx["amount_low"] == 15_001.0
        assert nflx["amount_high"] == 50_000.0

    def test_alford_ptr_scenario_with_exchange_filtered(self) -> None:
        """Modelled on ptr_20034201: AMZN/AAPL/T/BRK.B sales + 1 exchange dropped.

        Exchange is at input position 4; the 4 kept items are at positions 0-3.
        txn_idx values are 0,1,2,3 — same as before because no items precede them.
        """
        txns = [
            make_txn(doc_id="20034201", member_name="Mark Alford", ticker="AMZN", txn_type="S", amount_low=1_001.0, amount_high=15_000.0),    # pos 0
            make_txn(doc_id="20034201", member_name="Mark Alford", ticker="AAPL", txn_type="S", amount_low=15_001.0, amount_high=50_000.0),   # pos 1
            make_txn(doc_id="20034201", member_name="Mark Alford", ticker="T", txn_type="S", amount_low=1_001.0, amount_high=15_000.0),       # pos 2
            make_txn(doc_id="20034201", member_name="Mark Alford", ticker="BRK.B", txn_type="S", amount_low=15_001.0, amount_high=50_000.0), # pos 3
            make_txn(doc_id="20034201", member_name="Mark Alford", ticker="XYZ", txn_type="E", amount_low=1_001.0, amount_high=15_000.0),    # pos 4 — dropped
        ]
        result = to_raw_filings(txns)

        assert len(result) == 4  # exchange dropped
        tickers = [r["ticker"] for r in result]
        assert "XYZ" not in tickers
        assert result[3]["ticker"] == "BRK.B"
        # txn_idx must be the input positions 0-3 (exchange is at pos 4, last)
        assert [r["txn_idx"] for r in result] == [0, 1, 2, 3]
        # accession for last item (BRK.B, input pos 3)
        assert result[3]["accession"] == "H-20034201-3"

    def test_mixed_equity_and_nonequity_scenario(self) -> None:
        """Real scenario: Treasury (GS), partnership (PS), AAPL (ST), QQQ (OT)."""
        txns = [
            make_txn(doc_id="MIXED01", asset_type="GS", ticker="91282CNG2", txn_type="P"),  # pos 0 — dropped (GS)
            make_txn(doc_id="MIXED01", asset_type="PS", ticker="KRSOX",     txn_type="P"),  # pos 1 — dropped (PS)
            make_txn(doc_id="MIXED01", asset_type="ST", ticker="AAPL",      txn_type="P"),  # pos 2 — kept
            make_txn(doc_id="MIXED01", asset_type="OT", ticker="QQQ",       txn_type="S"),  # pos 3 — kept
        ]
        result = to_raw_filings(txns)
        assert len(result) == 2
        assert result[0]["ticker"] == "AAPL"
        assert result[0]["txn_idx"] == 2
        assert result[0]["accession"] == "H-MIXED01-2"
        assert result[1]["ticker"] == "QQQ"
        assert result[1]["txn_idx"] == 3
        assert result[1]["accession"] == "H-MIXED01-3"

    def test_cusip_leaked_as_ot_dropped(self) -> None:
        """A Treasury CUSIP tagged as OT (the live bug) must still be dropped."""
        txns = [
            make_txn(doc_id="CUSIP01", asset_type="OT", ticker="91282CNG2", txn_type="P"),  # CUSIP — dropped
            make_txn(doc_id="CUSIP01", asset_type="OT", ticker="QQQ",       txn_type="S"),  # real ETF — kept (pos 1)
        ]
        result = to_raw_filings(txns)
        assert len(result) == 1
        assert result[0]["ticker"] == "QQQ"
        assert result[0]["txn_idx"] == 1
        assert result[0]["accession"] == "H-CUSIP01-1"


# ---------------------------------------------------------------------------
# [C1] House Clerk receipt date → filing_ts (avoid look-ahead)
# ---------------------------------------------------------------------------


class TestClerkReceiptDate:
    """filing_ts must use the Clerk receipt date (public-availability) when set."""

    def test_clerk_date_used_for_filing_ts(self) -> None:
        """When clerk_receipt_date is later than notification_date, it wins."""
        txn = make_txn(
            txn_date=date(2025, 12, 12),
            notification_date=date(2026, 1, 6),   # member-notified (earlier)
            clerk_receipt_date=date(2026, 1, 15),  # publicly available (later)
        )
        result = to_raw_filings([txn])
        dt = datetime.fromisoformat(result[0]["filing_ts"])
        assert dt.date() == date(2026, 1, 15), (
            "filing_ts must be the Clerk receipt date (public-availability), "
            "not the earlier per-row notification date"
        )

    def test_no_clerk_date_falls_back_to_notification(self) -> None:
        """Senate-style txn (no clerk date) keeps using notification_date."""
        txn = make_txn(
            notification_date=date(2026, 2, 24),
            clerk_receipt_date=None,
        )
        result = to_raw_filings([txn])
        dt = datetime.fromisoformat(result[0]["filing_ts"])
        assert dt.date() == date(2026, 2, 24)

    def test_clerk_date_never_earlier_than_notification(self) -> None:
        """A corrupt earlier clerk date must not introduce look-ahead."""
        txn = make_txn(
            notification_date=date(2026, 3, 16),
            clerk_receipt_date=date(2026, 3, 1),  # implausibly earlier
        )
        result = to_raw_filings([txn])
        dt = datetime.fromisoformat(result[0]["filing_ts"])
        # Keep the LATER of the two so we never disclose before info was public.
        assert dt.date() == date(2026, 3, 16)

    def test_clerk_date_not_in_output_keyset(self) -> None:
        """Adding clerk_receipt_date must not change the shared RawFiling key-set."""
        txn = make_txn(clerk_receipt_date=date(2026, 1, 15))
        result = to_raw_filings([txn])
        assert set(result[0].keys()) == EXPECTED_KEYS


# ---------------------------------------------------------------------------
# [C3] Amendment referent — supersede only the original report it amends
# ---------------------------------------------------------------------------


class TestAmendmentReferent:
    """amendment_referent scopes supersession to the SAME original filing."""

    def test_non_amendment_has_no_referent(self) -> None:
        from arbiter.ingest.congress.normalize import amendment_referent
        txn = make_txn(is_amendment=False)
        assert amendment_referent(txn) is None

    def test_amendment_referent_is_person_plus_disclosure_date(self) -> None:
        from arbiter.ingest.congress.normalize import amendment_referent
        txn = make_txn(
            member_name="John Boozman",
            notification_date=date(2026, 6, 16),
            is_amendment=True,
        )
        assert amendment_referent(txn) == "congress:John Boozman:2026-06-16"

    def test_independent_same_ticker_filing_has_different_referent(self) -> None:
        """An amendment and an UNRELATED same-ticker filing get different referents,
        so a scoped supersede will not conflate them (the independent one survives)."""
        from arbiter.ingest.congress.normalize import amendment_referent
        amendment = make_txn(
            member_name="John Boozman",
            ticker="VEA",
            notification_date=date(2026, 6, 16),
            is_amendment=True,
        )
        # Same senator, SAME ticker, but a DIFFERENT report (different disclosure date)
        independent = make_txn(
            member_name="John Boozman",
            ticker="VEA",
            notification_date=date(2026, 2, 1),
            is_amendment=True,
        )
        assert amendment_referent(amendment) != amendment_referent(independent)

    def test_amendment_referent_embedded_in_raw_json(self) -> None:
        amendment = make_txn(
            member_name="John Boozman",
            notification_date=date(2026, 6, 16),
            is_amendment=True,
        )
        result = to_raw_filings([amendment], chamber_prefix="S")
        raw = json.loads(result[0]["raw_json"])
        assert raw["amends_referent"] == "congress:John Boozman:2026-06-16"

    def test_non_amendment_raw_json_has_no_referent(self) -> None:
        txn = make_txn(is_amendment=False)
        result = to_raw_filings([txn])
        raw = json.loads(result[0]["raw_json"])
        assert "amends_referent" not in raw
