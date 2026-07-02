"""Tests for the book-state exposure accumulator (W-RISKBOOK).

The ``RiskBook`` maintains running exposure across a decision cycle so the
engine can feed ``decide(current_open_positions=, current_gross_exposure=,
current_sector_exposure=)`` with real book state instead of zeros.

CRITICAL UNITS: all exposure is **notional USD market value** (PaperOrder.qty
is dollars in this path, NOT shares).
"""
from __future__ import annotations

import pytest

from arbiter.policy.book import RiskBook


def sector_for(ticker: str) -> str:
    """Toy sector mapper used across tests."""
    table = {
        "AAPL": "TECH",
        "MSFT": "TECH",
        "XOM": "ENERGY",
        "CVX": "ENERGY",
    }
    return table.get(ticker, "UNKNOWN")


# ---------------------------------------------------------------------------
# Empty book
# ---------------------------------------------------------------------------

def test_empty_book_is_all_zeros() -> None:
    book = RiskBook(held={}, sector_for=sector_for)
    assert book.open_positions() == 0
    assert book.gross_exposure() == 0.0
    assert book.sector_exposure() == {}


def test_empty_book_sector_lookup_returns_zero() -> None:
    book = RiskBook(held={}, sector_for=sector_for)
    assert book.sector_exposure_for("AAPL") == 0.0


# ---------------------------------------------------------------------------
# Seeding from held positions
# ---------------------------------------------------------------------------

def test_seed_count() -> None:
    book = RiskBook(
        held={"AAPL": 1000.0, "MSFT": 2000.0, "XOM": 500.0},
        sector_for=sector_for,
    )
    assert book.open_positions() == 3


def test_seed_gross_is_sum_of_notional_usd() -> None:
    book = RiskBook(
        held={"AAPL": 1000.0, "MSFT": 2000.0, "XOM": 500.0},
        sector_for=sector_for,
    )
    assert book.gross_exposure() == pytest.approx(3500.0)


def test_seed_sector_exposure_groups_by_sector() -> None:
    book = RiskBook(
        held={"AAPL": 1000.0, "MSFT": 2000.0, "XOM": 500.0, "CVX": 750.0},
        sector_for=sector_for,
    )
    assert book.sector_exposure() == pytest.approx(
        {"TECH": 3000.0, "ENERGY": 1250.0}
    )


def test_seed_sector_exposure_for_specific_ticker() -> None:
    book = RiskBook(
        held={"AAPL": 1000.0, "MSFT": 2000.0, "XOM": 500.0},
        sector_for=sector_for,
    )
    # current_sector_exposure for AAPL = whole TECH bucket
    assert book.sector_exposure_for("AAPL") == pytest.approx(3000.0)
    assert book.sector_exposure_for("XOM") == pytest.approx(500.0)
    # ticker not held but its sector is known -> sector total
    assert book.sector_exposure_for("CVX") == pytest.approx(500.0)


# ---------------------------------------------------------------------------
# add() — fold in a new order after a successful submit
# ---------------------------------------------------------------------------

def test_add_returns_new_book_original_unchanged() -> None:
    book = RiskBook(held={"AAPL": 1000.0}, sector_for=sector_for)
    book2 = book.add("MSFT", 500.0)
    # original immutable
    assert book.open_positions() == 1
    assert book.gross_exposure() == pytest.approx(1000.0)
    # new book accumulated
    assert book2.open_positions() == 2
    assert book2.gross_exposure() == pytest.approx(1500.0)


def test_add_accumulates_gross_and_sector() -> None:
    book = (
        RiskBook(held={}, sector_for=sector_for)
        .add("AAPL", 1000.0)
        .add("MSFT", 2000.0)
        .add("XOM", 500.0)
    )
    assert book.open_positions() == 3
    assert book.gross_exposure() == pytest.approx(3500.0)
    assert book.sector_exposure() == pytest.approx(
        {"TECH": 3000.0, "ENERGY": 500.0}
    )


def test_add_to_existing_ticker_adds_notional_not_new_position() -> None:
    """Adding to a ticker already in the book grows notional, not count."""
    book = RiskBook(held={"AAPL": 1000.0}, sector_for=sector_for)
    book2 = book.add("AAPL", 500.0)
    assert book2.open_positions() == 1  # still one position
    assert book2.gross_exposure() == pytest.approx(1500.0)
    assert book2.sector_exposure_for("AAPL") == pytest.approx(1500.0)


# ---------------------------------------------------------------------------
# Shape matches decide() params exactly
# ---------------------------------------------------------------------------

def test_feeds_decide_params() -> None:
    book = RiskBook(
        held={"AAPL": 1000.0, "MSFT": 2000.0, "XOM": 500.0},
        sector_for=sector_for,
    )
    kwargs = book.as_decide_kwargs("XOM")
    assert kwargs == {
        "current_open_positions": 3,
        "current_gross_exposure": pytest.approx(3500.0),
        "current_sector_exposure": pytest.approx(500.0),
        "current_name_exposure": pytest.approx(500.0),  # Tier-2 #5 add-on headroom
    }
    # types match decide() signature
    assert isinstance(kwargs["current_open_positions"], int)
    assert isinstance(kwargs["current_gross_exposure"], float)
    assert isinstance(kwargs["current_sector_exposure"], float)
    assert isinstance(kwargs["current_name_exposure"], float)
