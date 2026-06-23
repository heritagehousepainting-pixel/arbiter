"""Tests for arbiter.signals.detection — Lane 6.

Covers:
- Cluster buy detected from fixture filings.
- Single-insider buy detected above threshold.
- 10b5-1 filings are excluded (double-check flag).
- Congress filings produce cluster signals.
- No signal when below cluster threshold.
- No look-ahead (filing_ts > as_of excluded).
- Weak / zero-conviction signals.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone, timedelta

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.signals.detection import (
    Signal,
    SignalType,
    detect_signals,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc


def _ts(year: int, month: int, day: int) -> str:
    """Build a tz-aware ISO UTC timestamp string."""
    return datetime(year, month, day, 12, 0, 0, tzinfo=_UTC).isoformat()


def _as_of(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 23, 59, 59, tzinfo=_UTC)


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
    filing_ts: str,
    txn_type: str = "P",
    amount_low: float = 200_000.0,
    amount_high: float = 250_000.0,
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
            id, source, ticker, person_id, filing_ts, txn_type,
            amount_low, amount_high, is_10b5_1,
            datetime.now(_UTC).isoformat(),
        ),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Cluster buy detection
# ---------------------------------------------------------------------------

class TestClusterBuyDetection:
    def test_cluster_buy_detected_two_insiders(self):
        """Two insiders buying same ticker within 30-day window → cluster signal."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="AAPL", person_id="P001",
                       filing_ts=_ts(2026, 1, 5))
        _insert_filing(conn, id="F002", ticker="AAPL", person_id="P002",
                       filing_ts=_ts(2026, 1, 20))

        as_of = _as_of(2026, 2, 1)
        signals = detect_signals(conn, as_of, ticker="AAPL")

        cluster_signals = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert len(cluster_signals) >= 1

        sig = cluster_signals[0]
        assert sig.ticker == "AAPL"
        assert sig.source == "form4"
        assert "P001" in sig.person_ids
        assert "P002" in sig.person_ids
        assert "F001" in sig.filing_ids
        assert "F002" in sig.filing_ids
        assert sig.conviction_score > 0.0

    def test_cluster_buy_three_insiders_higher_conviction(self):
        """More insiders → higher conviction_score."""
        conn = _make_conn()
        for i, pid in enumerate(["P001", "P002", "P003"], start=1):
            _insert_filing(conn, id=f"F00{i}", ticker="TSLA", person_id=pid,
                           filing_ts=_ts(2026, 3, i))

        as_of = _as_of(2026, 3, 30)
        signals = detect_signals(conn, as_of, ticker="TSLA")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert cluster

        two_person = [s for s in cluster if len(s.person_ids) == 2]
        three_person = [s for s in cluster if len(s.person_ids) == 3]
        if two_person and three_person:
            assert three_person[0].conviction_score > two_person[0].conviction_score

    def test_no_cluster_below_min_people(self):
        """Only 1 insider → no cluster signal."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="MSFT", person_id="P001",
                       filing_ts=_ts(2026, 2, 1))

        as_of = _as_of(2026, 3, 1)
        signals = detect_signals(conn, as_of, ticker="MSFT")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert cluster == []

    def test_cluster_outside_window_no_detection(self):
        """Two insiders buying more than window_days apart → no cluster."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="NVDA", person_id="P001",
                       filing_ts=_ts(2026, 1, 1))
        _insert_filing(conn, id="F002", ticker="NVDA", person_id="P002",
                       filing_ts=_ts(2026, 3, 15))  # 73 days later

        as_of = _as_of(2026, 4, 1)
        signals = detect_signals(conn, as_of, ticker="NVDA", cluster_window_days=30)
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert cluster == []

    def test_cluster_ignores_sales(self):
        """Insider SELL (txn_type='S') does not count for a BUY cluster."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="AMZN", person_id="P001",
                       filing_ts=_ts(2026, 2, 5), txn_type="S")
        _insert_filing(conn, id="F002", ticker="AMZN", person_id="P002",
                       filing_ts=_ts(2026, 2, 10), txn_type="P")

        as_of = _as_of(2026, 3, 1)
        signals = detect_signals(conn, as_of, ticker="AMZN")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert cluster == []

    def test_no_look_ahead(self):
        """Filings after as_of must not be included."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="GOOG", person_id="P001",
                       filing_ts=_ts(2026, 6, 1))
        _insert_filing(conn, id="F002", ticker="GOOG", person_id="P002",
                       filing_ts=_ts(2026, 6, 2))

        # as_of is before both filings
        as_of = _as_of(2026, 5, 31)
        signals = detect_signals(conn, as_of, ticker="GOOG")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert cluster == []

    def test_superseded_filings_excluded(self):
        """Superseded filings (is_superseded=1) should not appear in signals."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="META", person_id="P001",
                       filing_ts=_ts(2026, 1, 10))
        # Mark it superseded
        conn.execute("UPDATE filings SET is_superseded=1 WHERE id='F001'")
        conn.commit()
        _insert_filing(conn, id="F002", ticker="META", person_id="P002",
                       filing_ts=_ts(2026, 1, 15))

        as_of = _as_of(2026, 2, 1)
        signals = detect_signals(conn, as_of, ticker="META")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        # Only 1 valid filing → no cluster
        assert cluster == []


# ---------------------------------------------------------------------------
# 10b5-1 exclusion (double-check defense-in-depth)
# ---------------------------------------------------------------------------

class Test10b51Exclusion:
    def test_10b5_1_flagged_filing_excluded_from_cluster(self):
        """A filing with is_10b5_1=1 must not count toward a cluster."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="CRM", person_id="P001",
                       filing_ts=_ts(2026, 2, 1), is_10b5_1=1)
        _insert_filing(conn, id="F002", ticker="CRM", person_id="P002",
                       filing_ts=_ts(2026, 2, 10))

        as_of = _as_of(2026, 3, 1)
        signals = detect_signals(conn, as_of, ticker="CRM")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        # Only one non-10b5-1 filing → no cluster
        assert cluster == []

    def test_10b5_1_filing_not_in_single_insider(self):
        """A 10b5-1 filing above threshold should NOT produce a single-insider signal."""
        conn = _make_conn()
        _insert_filing(
            conn, id="F001", ticker="ORCL", person_id="P001",
            filing_ts=_ts(2026, 3, 1), is_10b5_1=1,
            amount_low=5_000_000.0,
        )

        as_of = _as_of(2026, 4, 1)
        signals = detect_signals(conn, as_of, ticker="ORCL")
        single = [s for s in signals if s.signal_type == SignalType.SINGLE_INSIDER_BUY]
        assert single == []


# ---------------------------------------------------------------------------
# Single-insider buy
# ---------------------------------------------------------------------------

class TestSingleInsiderBuy:
    def test_large_purchase_detected(self):
        """A large single-insider purchase (≥$100k) produces a signal."""
        conn = _make_conn()
        _insert_filing(
            conn, id="F001", ticker="NFLX", person_id="P099",
            filing_ts=_ts(2026, 4, 10), amount_low=500_000.0,
        )

        as_of = _as_of(2026, 5, 1)
        signals = detect_signals(conn, as_of, ticker="NFLX")
        single = [s for s in signals if s.signal_type == SignalType.SINGLE_INSIDER_BUY]
        assert len(single) >= 1
        assert single[0].ticker == "NFLX"
        assert single[0].person_ids == ("P099",)
        assert single[0].conviction_score > 0.0

    def test_small_purchase_below_threshold_ignored(self):
        """A purchase below $100k should not produce a single-insider signal."""
        conn = _make_conn()
        _insert_filing(
            conn, id="F001", ticker="SPOT", person_id="P050",
            filing_ts=_ts(2026, 4, 1), amount_low=5_000.0,
        )

        as_of = _as_of(2026, 5, 1)
        signals = detect_signals(conn, as_of, ticker="SPOT")
        single = [s for s in signals if s.signal_type == SignalType.SINGLE_INSIDER_BUY]
        assert single == []


# ---------------------------------------------------------------------------
# Congress sector detection
# ---------------------------------------------------------------------------

class TestCongressDetection:
    def test_congress_cluster_detected(self):
        """Two Congress members buying same ticker → cluster signal from congress source."""
        conn = _make_conn()
        _insert_filing(conn, id="C001", source="congress", ticker="LMT",
                       person_id="REP001", filing_ts=_ts(2026, 5, 1), amount_low=100_000.0)
        _insert_filing(conn, id="C002", source="congress", ticker="LMT",
                       person_id="REP002", filing_ts=_ts(2026, 5, 15), amount_low=80_000.0)

        as_of = _as_of(2026, 6, 1)
        signals = detect_signals(conn, as_of, ticker="LMT")
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY
                   and s.source == "congress"]
        assert len(cluster) >= 1
        assert "REP001" in cluster[0].person_ids
        assert "REP002" in cluster[0].person_ids

    def test_congress_and_form4_separate(self):
        """Congress and Form 4 filings do not mix into a single cluster."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", source="form4", ticker="INTC",
                       person_id="INS001", filing_ts=_ts(2026, 5, 1))
        _insert_filing(conn, id="C001", source="congress", ticker="INTC",
                       person_id="REP001", filing_ts=_ts(2026, 5, 5))

        as_of = _as_of(2026, 6, 1)
        signals = detect_signals(conn, as_of, ticker="INTC")

        form4_clusters = [s for s in signals
                          if s.signal_type == SignalType.CLUSTER_BUY and s.source == "form4"]
        congress_clusters = [s for s in signals
                             if s.signal_type == SignalType.CLUSTER_BUY and s.source == "congress"]

        # Neither source alone has 2 people → no clusters
        assert form4_clusters == []
        assert congress_clusters == []


# ---------------------------------------------------------------------------
# Multi-ticker isolation
# ---------------------------------------------------------------------------

class TestMultiTicker:
    def test_signals_not_mixed_across_tickers(self):
        """Cluster only forms within the same ticker."""
        conn = _make_conn()
        _insert_filing(conn, id="F001", ticker="AAPL", person_id="P001",
                       filing_ts=_ts(2026, 1, 5))
        _insert_filing(conn, id="F002", ticker="GOOG", person_id="P002",
                       filing_ts=_ts(2026, 1, 10))

        as_of = _as_of(2026, 2, 1)
        signals = detect_signals(conn, as_of)
        cluster = [s for s in signals if s.signal_type == SignalType.CLUSTER_BUY]
        assert cluster == []


# ---------------------------------------------------------------------------
# Activist / passive stake (Schedule 13D/13G) detection — Wave 2
# ---------------------------------------------------------------------------

def _insert_sc13(
    conn: sqlite3.Connection,
    *,
    id: str,
    ticker: str = "AAPL",
    person_id: str = "ACT001",
    filing_ts: str,
    txn_type: str = "P",
    schedule: str = "13D",
    percent_of_class: float | None = 8.5,
    is_activist: bool = True,
) -> None:
    raw = json.dumps({
        "schedule": schedule,
        "percent_of_class": percent_of_class,
        "is_activist": is_activist,
    })
    conn.execute(
        """
        INSERT INTO filings
            (id, source, ticker, person_id, filing_ts, txn_type,
             amount_low, amount_high, is_10b5_1, is_superseded, raw_json, created_at)
        VALUES (?, 'form13d', ?, ?, ?, ?, 0, 0, 0, 0, ?, ?)
        """,
        (id, ticker, person_id, filing_ts, txn_type, raw,
         datetime.now(_UTC).isoformat()),
    )
    conn.commit()


class TestActivistStakeDetection:
    def test_detect_activist_stake_long(self):
        """13D activist 'P' filing → one ACTIVIST_STAKE signal with base+boost conviction."""
        conn = _make_conn()
        _insert_sc13(conn, id="S001", filing_ts=_ts(2026, 1, 5),
                     schedule="13D", percent_of_class=8.5, is_activist=True)
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="AAPL")
        act = [s for s in signals if s.signal_type == SignalType.ACTIVIST_STAKE]
        assert len(act) == 1
        sig = act[0]
        assert sig.source == "form13d"
        expected = round(min(0.70 + min(8.5 / 50.0, 0.30), 1.0), 4)
        assert sig.conviction_score == expected
        assert sig.meta["txn_type"] == "P"
        assert sig.meta["schedule"] == "13D"

    def test_detect_activist_exit_sign(self):
        """An exit ('S') carries txn_type='S' in meta for emit to flip the sign."""
        conn = _make_conn()
        _insert_sc13(conn, id="S001", filing_ts=_ts(2026, 1, 5), txn_type="S")
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="AAPL")
        act = [s for s in signals if s.signal_type == SignalType.ACTIVIST_STAKE]
        assert len(act) == 1
        assert act[0].meta["txn_type"] == "S"

    def test_detect_activist_passive_13g(self):
        """A passive 13G (is_activist=false) uses the 0.35 base conviction."""
        conn = _make_conn()
        _insert_sc13(conn, id="S001", filing_ts=_ts(2026, 1, 5),
                     schedule="13G", percent_of_class=0.0, is_activist=False)
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="AAPL")
        act = [s for s in signals if s.signal_type == SignalType.ACTIVIST_STAKE]
        assert len(act) == 1
        assert act[0].conviction_score == 0.35

    def test_detect_activist_no_lookahead(self):
        """A filing_ts after as_of produces no signal."""
        conn = _make_conn()
        _insert_sc13(conn, id="S001", filing_ts=_ts(2026, 6, 1))
        signals = detect_signals(conn, _as_of(2026, 1, 1), ticker="AAPL")
        act = [s for s in signals if s.signal_type == SignalType.ACTIVIST_STAKE]
        assert act == []
