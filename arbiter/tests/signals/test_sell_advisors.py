"""Tier-3 #9 — insider/congress cluster-SELL detection + emission (2026-07-02).

The bearish disclosure leg: form4/congress txn_type='S' rows (previously never
fetched) now feed CLUSTER_SELL / CONGRESS_SELL signals.  Noise filter: sells
require one MORE distinct person than buy clusters (default 2+1=3).  Emit maps
the sell types to their OWN advisor ids (A1.insider_sell / A1.congress_sell)
at the 90d MEDIUM horizon with a NEGATIVE stance.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.signals.detection import SignalType, detect_signals
from arbiter.signals.emit import emit_opinion

_UTC = timezone.utc


def _ts(year: int, month: int, day: int) -> str:
    return datetime(year, month, day, 12, 0, 0, tzinfo=_UTC).isoformat()


def _as_of(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, 23, 59, 59, tzinfo=_UTC)


def _make_conn() -> sqlite3.Connection:
    conn = get_connection(":memory:")
    run_migrations(conn)
    return conn


def _insert_filing(conn, *, id, source="form4", ticker="AAPL", person_id="P001",
                   filing_ts, txn_type="S", amount_low=200_000.0):
    conn.execute(
        """
        INSERT INTO filings
            (id, source, ticker, person_id, filing_ts, txn_type,
             amount_low, amount_high, is_10b5_1, is_superseded, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?)
        """,
        (id, source, ticker, person_id, filing_ts, txn_type,
         amount_low, amount_low * 1.2, datetime.now(_UTC).isoformat()),
    )
    conn.commit()


def _seed_sellers(conn, n, *, source="form4", ticker="AAPL"):
    for i in range(n):
        _insert_filing(conn, id=f"S{i:03d}", source=source, ticker=ticker,
                       person_id=f"P{i:03d}", filing_ts=_ts(2026, 1, 5 + i))


class TestClusterSellDetection:
    def test_three_sellers_fire_cluster_sell(self):
        conn = _make_conn()
        _seed_sellers(conn, 3)
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="AAPL")
        sells = [s for s in signals if s.signal_type == SignalType.CLUSTER_SELL]
        assert len(sells) >= 1
        sig = sells[0]
        assert sig.source == "form4"
        assert sig.meta["txn_type"] == "S"
        assert len(sig.person_ids) == 3

    def test_two_sellers_below_noise_floor(self):
        """Sells need min_people+1 (3 by default) — 2 sellers is noise."""
        conn = _make_conn()
        _seed_sellers(conn, 2)
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="AAPL")
        assert [s for s in signals if s.signal_type == SignalType.CLUSTER_SELL] == []

    def test_congress_sells_fire_congress_sell(self):
        conn = _make_conn()
        _seed_sellers(conn, 3, source="congress", ticker="NVDA")
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="NVDA")
        sells = [s for s in signals if s.signal_type == SignalType.CONGRESS_SELL]
        assert len(sells) >= 1
        assert sells[0].source == "congress"

    def test_buy_clusters_unaffected(self):
        """Regression: 2 buyers still fire CLUSTER_BUY; sells don't pollute it."""
        conn = _make_conn()
        _insert_filing(conn, id="B1", person_id="P900",
                       filing_ts=_ts(2026, 1, 5), txn_type="P")
        _insert_filing(conn, id="B2", person_id="P901",
                       filing_ts=_ts(2026, 1, 10), txn_type="P")
        _seed_sellers(conn, 3)
        signals = detect_signals(conn, _as_of(2026, 2, 1), ticker="AAPL")
        types = {s.signal_type for s in signals}
        assert SignalType.CLUSTER_BUY in types
        assert SignalType.CLUSTER_SELL in types


class TestSellEmission:
    def _best_sell(self, source="form4", ticker="AAPL"):
        conn = _make_conn()
        _seed_sellers(conn, 3, source=source, ticker=ticker)
        as_of = _as_of(2026, 2, 1)
        wanted = (SignalType.CLUSTER_SELL if source == "form4"
                  else SignalType.CONGRESS_SELL)
        signals = [s for s in detect_signals(conn, as_of, ticker=ticker)
                   if s.signal_type == wanted]
        return emit_opinion(signals[0], as_of)

    def test_insider_sell_opinion(self):
        op = self._best_sell()
        assert op is not None
        assert op.advisor_id == "A1.insider_sell"
        assert op.stance_score < 0.0  # bearish
        assert op.horizon_days == 90  # MEDIUM — must match the engine's idea bucket
        assert "selling" in op.rationale

    def test_congress_sell_opinion(self):
        op = self._best_sell(source="congress", ticker="NVDA")
        assert op is not None
        assert op.advisor_id == "A1.congress_sell"
        assert op.stance_score < 0.0
        assert op.horizon_days == 90

    def test_buy_rationale_still_says_buying(self):
        conn = _make_conn()
        _insert_filing(conn, id="B1", person_id="P900",
                       filing_ts=_ts(2026, 1, 5), txn_type="P")
        _insert_filing(conn, id="B2", person_id="P901",
                       filing_ts=_ts(2026, 1, 10), txn_type="P")
        as_of = _as_of(2026, 2, 1)
        buys = [s for s in detect_signals(conn, as_of, ticker="AAPL")
                if s.signal_type == SignalType.CLUSTER_BUY]
        op = emit_opinion(buys[0], as_of)
        assert op is not None
        assert op.stance_score > 0.0
        assert "buying" in op.rationale


class TestEngineWiring:
    def test_advisor_map_includes_sell_advisors(self, tmp_path, monkeypatch):
        """build_engine registers the two sell advisors."""
        import dataclasses

        from arbiter.config import load_config
        from arbiter.data.clock import BacktestClock
        from arbiter.engine import build_engine

        config = dataclasses.replace(
            load_config(), executor_backend="sim",
            db_path=str(tmp_path / "e.db"),
            audit_path=str(tmp_path / "a.jsonl"),
            metrics_path=str(tmp_path / "m.jsonl"),
            kill_switch_url="", alert_webhook_url="",
        )
        eng = build_engine(config, clock=BacktestClock(_as_of(2026, 2, 1)))
        assert "A1.insider_sell" in eng.advisor_map
        assert "A1.congress_sell" in eng.advisor_map
