"""Tests for arbiter.evaluation.backfill — W-BACKFILL.

OFFLINE only: fake PIT (FixtureSource) with historical bars, fixture filings,
and a BacktestClock-derived cutoff.  No network.

Covers:
- Backfilling historical filings whose horizons have elapsed mints the expected
  ResolvedOutcome rows with correct alpha sign / stance.
- PIT-cleanliness: an idea whose horizon has NOT elapsed by the cutoff is not
  minted (no look-ahead beyond the label window).
- Idempotent: a re-run at the same cutoff mints nothing new.
- The minted outcomes are consumable by the trust ledger (spot-check).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.evaluation.backfill import BackfillReport, backfill_outcomes
from arbiter.evaluation.outcome_store import query_outcomes

_UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _dt(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 12, 0, 0, tzinfo=_UTC)


def _make_conn() -> sqlite3.Connection:
    conn = get_connection(":memory:")
    run_migrations(conn)
    return conn


def _insert_filing(
    conn: sqlite3.Connection,
    *,
    id: str,
    source: str = "form4",
    ticker: str = "AAPL",
    person_id: str = "P001",
    filing_ts: datetime,
    txn_type: str = "P",
    amount_low: float = 500_000.0,
    amount_high: float = 750_000.0,
    is_10b5_1: int = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO filings
            (id, source, ticker, person_id, filing_ts, txn_type,
             amount_low, amount_high, is_10b5_1, is_superseded, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
        """,
        (
            id, source, ticker, person_id, filing_ts.isoformat(), txn_type,
            amount_low, amount_high, is_10b5_1,
            filing_ts.isoformat(),
        ),
    )
    conn.commit()


def _build_pit(
    *,
    ticker: str,
    entry_dt: datetime,
    horizon_days: int,
    ticker_entry: float,
    ticker_exit: float,
    spy_entry: float = 400.0,
    spy_exit: float = 400.0,
    beta: float = 1.0,
) -> PITGateway:
    """Build a PIT gateway with dense daily bars so every trading-day read resolves.

    We add a price_open / price_close point for EVERY calendar day from a week
    before entry through a week past the horizon end, so whatever exact trading
    day the labeler picks finds a value.  Entry/exit values are pinned on the
    specific entry day and horizon-end day; intervening days carry the entry
    value (flat) so returns are driven only by the pinned exit.
    """
    pit = PITGateway()
    open_src = FixtureSource()
    close_src = FixtureSource()
    beta_src = FixtureSource()
    spy_open_src = FixtureSource()
    spy_close_src = FixtureSource()

    horizon_end = entry_dt + timedelta(days=horizon_days + 2)
    cur = entry_dt - timedelta(days=10)
    while cur <= horizon_end + timedelta(days=10):
        # ticker
        open_src.add("price_open", ticker, cur, ticker_entry)
        close_src.add("price_close", ticker, cur, ticker_entry)
        # SPY
        spy_open_src.add("price_open", "SPY", cur, spy_entry)
        spy_close_src.add("price_close", "SPY", cur, spy_entry)
        cur = cur + timedelta(days=1)

    # Pin the exit value at and after the horizon-end window.
    exit_window_start = entry_dt + timedelta(days=horizon_days - 5)
    cur = exit_window_start
    while cur <= horizon_end + timedelta(days=10):
        close_src.add("price_close", ticker, cur, ticker_exit)
        spy_close_src.add("price_close", "SPY", cur, spy_exit)
        cur = cur + timedelta(days=1)

    # beta as_of (constant)
    beta_src.add("beta_252d", ticker, entry_dt - timedelta(days=400), beta)

    # Merge ticker + SPY into the same field sources.
    for src, field in (
        (open_src, "price_open"),
        (close_src, "price_close"),
    ):
        # also fold spy points into the same source
        pass
    # Combine ticker and SPY sources by registering a composite source.
    open_combined = FixtureSource()
    close_combined = FixtureSource()
    cur = entry_dt - timedelta(days=10)
    while cur <= horizon_end + timedelta(days=10):
        open_combined.add("price_open", ticker, cur, ticker_entry)
        open_combined.add("price_open", "SPY", cur, spy_entry)
        close_combined.add("price_close", ticker, cur, ticker_entry)
        close_combined.add("price_close", "SPY", cur, spy_entry)
        cur = cur + timedelta(days=1)
    cur = exit_window_start
    while cur <= horizon_end + timedelta(days=10):
        close_combined.add("price_close", ticker, cur, ticker_exit)
        close_combined.add("price_close", "SPY", cur, spy_exit)
        cur = cur + timedelta(days=1)

    pit.register_source("price_open", open_combined)
    pit.register_source("price_close", close_combined)
    pit.register_source("beta_252d", beta_src)
    return pit


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_backfill_mints_outcome_for_elapsed_horizon():
    """A single-insider buy whose 180d horizon elapsed mints one outcome."""
    conn = _make_conn()
    filing_dt = _dt(2024, 1, 8)
    _insert_filing(conn, id="F1", ticker="AAPL", filing_ts=filing_dt)

    # Form4 horizon = 180d.  Entry = filing+1 trading day.
    pit = _build_pit(
        ticker="AAPL",
        entry_dt=filing_dt,
        horizon_days=180,
        ticker_entry=100.0,
        ticker_exit=120.0,   # +20% raw, SPY flat → strongly positive alpha
    )

    cutoff = BacktestClock(_dt(2025, 1, 1)).now()  # well past horizon end
    report = backfill_outcomes(conn, pit, cutoff_as_of=cutoff)

    assert isinstance(report, BackfillReport)
    assert report.n_outcomes_minted == 1

    rows = query_outcomes(conn)
    assert len(rows) == 1
    row = rows[0]
    assert row["ticker"] == "AAPL"
    assert row["advisor_id"] == "A1.insider"
    assert row["alpha_bps"] > 0          # +20% with flat SPY
    assert row["binary"] == 1
    assert row["stance_score"] > 0       # carried from the emitted opinion


def test_backfill_pit_clean_skips_unelapsed_horizon():
    """A filing whose horizon has NOT elapsed by the cutoff mints nothing."""
    conn = _make_conn()
    filing_dt = _dt(2024, 6, 1)
    _insert_filing(conn, id="F1", ticker="AAPL", filing_ts=filing_dt)

    pit = _build_pit(
        ticker="AAPL",
        entry_dt=filing_dt,
        horizon_days=180,
        ticker_entry=100.0,
        ticker_exit=120.0,
    )

    # Cutoff is only ~30 days after the filing — 180d horizon NOT elapsed.
    cutoff = filing_dt + timedelta(days=30)
    report = backfill_outcomes(conn, pit, cutoff_as_of=cutoff)

    assert report.n_outcomes_minted == 0
    assert report.n_skipped_unelapsed >= 1
    assert query_outcomes(conn) == []


def test_backfill_idempotent_rerun_mints_nothing_new():
    """Re-running at the same cutoff does not double-write outcomes."""
    conn = _make_conn()
    filing_dt = _dt(2024, 1, 8)
    _insert_filing(conn, id="F1", ticker="AAPL", filing_ts=filing_dt)

    pit = _build_pit(
        ticker="AAPL",
        entry_dt=filing_dt,
        horizon_days=180,
        ticker_entry=100.0,
        ticker_exit=110.0,
    )
    cutoff = _dt(2025, 1, 1)

    r1 = backfill_outcomes(conn, pit, cutoff_as_of=cutoff)
    assert r1.n_outcomes_minted == 1

    r2 = backfill_outcomes(conn, pit, cutoff_as_of=cutoff)
    assert r2.n_outcomes_minted == 0
    assert r2.n_skipped_existing >= 1

    # Still exactly one outcome row.
    assert len(query_outcomes(conn)) == 1


def test_minted_outcome_consumable_by_trust_ledger():
    """The minted ResolvedOutcome can be fed to the trust ledger's update path."""
    from arbiter.contract.seams import ResolvedOutcome
    from arbiter.trust.ledger import TrustLedger

    conn = _make_conn()
    # Several distinct filings → several minted outcomes for one advisor.
    for i in range(8):
        _insert_filing(
            conn,
            id=f"F{i}",
            ticker="AAPL",
            person_id=f"P{i}",
            filing_ts=_dt(2024, 1, 2) + timedelta(days=i),
        )

    pit = _build_pit(
        ticker="AAPL",
        entry_dt=_dt(2024, 1, 2),
        horizon_days=180,
        ticker_entry=100.0,
        ticker_exit=115.0,
    )
    cutoff = _dt(2025, 1, 1)
    report = backfill_outcomes(conn, pit, cutoff_as_of=cutoff)
    assert report.n_outcomes_minted >= 1

    rows = query_outcomes(conn)
    # Reconstruct ResolvedOutcome objects and group by advisor for the ledger.
    outcomes_by_advisor: dict[str, list[tuple[ResolvedOutcome, datetime]]] = {}
    for r in rows:
        oc = ResolvedOutcome(
            idea_id=r["idea_id"],
            advisor_id=r["advisor_id"],
            ticker=r["ticker"],
            alpha_bps=r["alpha_bps"],
            binary=r["binary"],
            advisor_confidence=r["advisor_confidence"],
            stance_score=r["stance_score"],
            abstained=bool(r["abstained"]),
            horizon_days=r["horizon_days"],
            label_kind=r["label_kind"],
        )
        resolved_at = datetime.fromisoformat(r["created_at"])
        outcomes_by_advisor.setdefault(oc.advisor_id, []).append((oc, resolved_at))

    ledger = TrustLedger()
    bundle = ledger.update(
        outcomes_by_advisor=outcomes_by_advisor,
        eligible_by_advisor={a: [o.idea_id for o, _ in v] for a, v in outcomes_by_advisor.items()},
        as_of=cutoff,
        force=True,
    )
    # The ledger consumed the minted outcomes without error and produced a bundle.
    assert bundle is not None
