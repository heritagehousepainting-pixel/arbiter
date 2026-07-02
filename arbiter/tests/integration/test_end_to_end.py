"""End-to-end integration test — Wave C.

Exercises the full paper-sim cycle:
  seed filings → detect signals → emit opinions → fuse → decide → submit

Assertions (per ROADMAP.md "Done-when"):
1. LIVE_TRADING is False; executor is SimExecutor.
2. A position exists after running the cycle (order filled).
3. An ``orders`` row exists with a ``dedup_hash``.
4. An ``audit.jsonl`` entry was written.
5. Leaderboard renders without error.
6. Re-running the same cycle is idempotent (no duplicate order row).
7. A planted tripped breaker blocks new orders.
8. No look-ahead: a filing dated AFTER as_of is not detected.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.audit import read_audit
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.ingest.writer import write_filing
from arbiter.safety.breakers import CircuitBreaker
from arbiter.shared.sim_executor import SimExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_UTC = timezone.utc

# A fixed as_of in the past so filings are clearly "known before".
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


def _make_filing_ts(days_before_as_of: int) -> str:
    """Return an ISO timestamp that is ``days_before_as_of`` days before _AS_OF."""
    return (_AS_OF - timedelta(days=days_before_as_of)).isoformat()


def _make_future_filing_ts(days_after_as_of: int) -> str:
    """Return an ISO timestamp AFTER as_of (look-ahead canary)."""
    return (_AS_OF + timedelta(days=days_after_as_of)).isoformat()


def _seed_cluster_buy(
    conn: sqlite3.Connection,
    clock_fn: "callable",
    ticker: str = "AAPL",
    n_buyers: int = 3,
    amount: float = 500_000.0,
) -> list[str]:
    """Insert n_buyers distinct insider Form 4 buys within 30 days of as_of."""
    filing_ids = []
    for i in range(n_buyers):
        person_id = generate_ulid()
        raw = {
            "source": "form4",
            "ticker": ticker,
            "person_id": person_id,
            "filing_ts": _make_filing_ts(days_before_as_of=5 + i),  # 5, 6, 7 days before
            "txn_type": "P",
            "shares": 1000.0,
            "price": 150.0,
            "amount_low": amount,
            "amount_high": amount * 1.2,
            "is_10b5_1": False,
            "is_amendment": False,
            "accession": generate_ulid(),
            "raw_json": None,
        }
        fid = write_filing(conn, raw, clock_fn)
        if fid:
            filing_ids.append(fid)
    return filing_ids


def _build_pit_with_price(ticker: str = "AAPL") -> PITGateway:
    """Build a PITGateway seeded with a price for the given ticker."""
    fixture = FixtureSource()
    # Seed price and spread so the engine can compute sizing.
    ts_seed = _AS_OF - timedelta(days=1)
    fixture.add("price_close", ticker, ts_seed, 150.0)
    fixture.add("price_open", ticker, ts_seed, 150.0)
    fixture.add("spread", ticker, ts_seed, 0.01)
    # Seed adv_20d directly so sizing doesn't get a None from the computed source.
    fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)

    pit = PITGateway()
    pit.register_source("price_close", fixture)
    pit.register_source("price_open", fixture)
    pit.register_source("spread", fixture)
    pit.register_source("adv_20d", fixture)
    return pit


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def tmp_audit(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


@pytest.fixture()
def engine_and_conn(tmp_path: Path, tmp_audit: Path):
    """Build a fully wired Engine with tmp db + BacktestClock + FixtureSource PIT."""
    db_path = str(tmp_path / "test.db")

    # Build a config with paper-only, tmp paths.
    config = load_config()
    # Override mutable fields via object replacement (Config is frozen).
    import dataclasses
    config = dataclasses.replace(
        config,
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_audit),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )

    # BacktestClock fixed at _AS_OF.
    clock = BacktestClock(_AS_OF)

    # Use a direct connection so we can inspect the DB in the test.
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())

    # Seed filings.
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker="AAPL", n_buyers=3)

    # Build PIT.
    pit = _build_pit_with_price("AAPL")

    # Build engine with injected dependencies.
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)

    return eng, conn, config, tmp_audit


# ---------------------------------------------------------------------------
# Test 1 — paper-only assertion
# ---------------------------------------------------------------------------

def test_executor_is_sim(engine_and_conn):
    eng, conn, config, audit_path = engine_and_conn
    assert config.live_trading is False, "LIVE_TRADING must be False for integration test"
    assert isinstance(eng.executor, SimExecutor), (
        f"Expected SimExecutor, got {type(eng.executor)}"
    )
    assert eng.executor.name == "sim"


# ---------------------------------------------------------------------------
# Test 2 — full cycle: order placed, position exists, audit written
# ---------------------------------------------------------------------------

def test_cycle_places_order_and_writes_audit(engine_and_conn):
    eng, conn, config, audit_path = engine_and_conn

    result = eng.run_cycle(as_of=_AS_OF)

    # At least one idea was processed.
    assert result.ideas_processed >= 1, f"Expected >=1 ideas, got {result.ideas_processed}"

    # At least one order was submitted.
    assert result.orders_submitted >= 1, (
        f"Expected >=1 orders submitted, got {result.orders_submitted}. "
        f"opinions_gathered={result.opinions_gathered}, "
        f"opinions_null={result.opinions_null}, "
        f"errors={result.errors}"
    )

    # Position exists in SimExecutor.
    positions = eng.executor.get_positions()
    assert len(positions) >= 1, f"Expected open positions, got {positions}"

    # Order row exists in DB with a dedup_hash.
    rows = conn.execute("SELECT order_id, dedup_hash, ticker FROM orders").fetchall()
    assert len(rows) >= 1, "Expected at least 1 row in orders table"
    row = rows[0]
    assert row["dedup_hash"], "dedup_hash must be non-empty"
    assert row["ticker"] == "AAPL", f"Expected AAPL, got {row['ticker']}"

    # Audit entries written.
    entries = read_audit(str(audit_path))
    assert len(entries) >= 1, "Expected at least 1 audit.jsonl entry"
    events = {e["event"] for e in entries}
    assert "order.submitted" in events, f"'order.submitted' not in audit events: {events}"


# ---------------------------------------------------------------------------
# Test 3 — idempotency: re-running the same cycle produces no duplicate order
# ---------------------------------------------------------------------------

def test_cycle_is_idempotent(engine_and_conn):
    eng, conn, config, audit_path = engine_and_conn

    # First run.
    result1 = eng.run_cycle(as_of=_AS_OF)
    rows_after_first = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    # Second run with the same as_of.
    result2 = eng.run_cycle(as_of=_AS_OF)
    rows_after_second = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]

    # No new rows should be added (dedup_hash idempotency).
    assert rows_after_second == rows_after_first, (
        f"Expected idempotent: rows={rows_after_first} before, {rows_after_second} after re-run"
    )


# ---------------------------------------------------------------------------
# Test 4 — tripped breaker blocks new orders
# ---------------------------------------------------------------------------

def test_tripped_breaker_blocks_orders(engine_and_conn):
    eng, conn, config, audit_path = engine_and_conn

    # Plant a tripped breaker before the cycle.
    from arbiter.safety.breakers import CircuitBreaker
    cb = CircuitBreaker()
    cb.trip(
        "daily_loss",
        reason="test: manual trip",
        conn=conn,
        clock=BacktestClock(_AS_OF),
        audit_path=str(audit_path),
    )

    # Verify breaker is tripped.
    tripped = cb.any_tripped(conn)
    assert "daily_loss" in tripped, f"Expected daily_loss tripped, got {tripped}"

    # Run cycle — gate must block orders.
    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted == 0, (
        f"Expected 0 orders when breaker is tripped, got {result.orders_submitted}"
    )


# ---------------------------------------------------------------------------
# Test 5 — no look-ahead: filing dated AFTER as_of is ignored
# ---------------------------------------------------------------------------

def test_no_lookahead_future_filing_ignored(tmp_path: Path):
    """A filing timestamped after as_of must NOT appear in detected signals."""
    db_path = str(tmp_path / "lookahead_test.db")
    audit_path = str(tmp_path / "audit.jsonl")

    config = load_config()
    import dataclasses
    config = dataclasses.replace(
        config,
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=audit_path,
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )

    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())

    # Insert ONLY a future filing (should be invisible at as_of).
    future_ts = _make_future_filing_ts(days_after_as_of=1)
    for i in range(3):
        raw = {
            "source": "form4",
            "ticker": "FUTR",
            "person_id": generate_ulid(),
            "filing_ts": future_ts,
            "txn_type": "P",
            "shares": 1000.0,
            "price": 50.0,
            "amount_low": 200_000.0,
            "amount_high": 250_000.0,
            "is_10b5_1": False,
            "is_amendment": False,
            "accession": generate_ulid(),
            "raw_json": None,
        }
        write_filing(conn, raw, lambda: _AS_OF.isoformat())

    # Run signal detection — future filing should NOT appear.
    from arbiter.signals.detection import detect_signals
    signals = detect_signals(conn, _AS_OF)
    future_signals = [s for s in signals if s.ticker == "FUTR"]
    assert len(future_signals) == 0, (
        f"Look-ahead violation: future filing for FUTR was detected as a signal: {future_signals}"
    )

    conn.close()


# ---------------------------------------------------------------------------
# Test 6 — leaderboard renders
# ---------------------------------------------------------------------------

def test_leaderboard_renders(engine_and_conn):
    eng, conn, config, audit_path = engine_and_conn
    board = eng.leaderboard(as_of=_AS_OF)

    assert isinstance(board, str), "leaderboard() must return a string"
    assert "A1 Signal Leaderboard" in board, "Expected leaderboard header"
    assert "cluster_buy" in board, "Expected cluster_buy signal type row"


# ---------------------------------------------------------------------------
# Test 7 — status() returns expected keys
# ---------------------------------------------------------------------------

def test_status_returns_correct_fields(engine_and_conn):
    eng, conn, config, audit_path = engine_and_conn
    info = eng.status()

    assert info["live_trading"] is False
    assert info["is_sim"] is True
    assert info["executor"] == "sim"
    assert "advisor_count" in info
    assert info["advisor_count"] == 6  # 4 buy-side A1 + 2 sell legs (Tier-3 #9)
    assert "A1.insider" in info["advisors"]
    assert "A1.congress" in info["advisors"]
    assert "A1.activist" in info["advisors"]
    assert "A1.fund" in info["advisors"]


# ---------------------------------------------------------------------------
# Test 8 (Finding 1) — kill switch halt blocks order submission
# ---------------------------------------------------------------------------

def test_kill_switch_halt_blocks_all_orders(tmp_path: Path):
    """A halted kill switch must prevent any orders from being submitted.

    Finding 1 fix: KillSwitch.is_halted() is now called BEFORE the breaker
    check and before gathering opinions.  A halted switch returns immediately
    with ideas_processed=0 and no DB rows.
    """
    import dataclasses
    from unittest.mock import MagicMock

    db_path = str(tmp_path / "kill_switch_test.db")
    audit_path = str(tmp_path / "audit.jsonl")

    config = load_config()
    config = dataclasses.replace(
        config,
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=audit_path,
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="https://fake-kill-switch.example.com",  # configured URL
    )

    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())

    # Seed filings so there are signals to detect.
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker="AAPL", n_buyers=3)

    pit = _build_pit_with_price("AAPL")

    # Build a mock KillSwitch that always reports HALTED.
    from arbiter.safety.kill_switch import KillSwitch
    mock_ks = MagicMock(spec=KillSwitch)
    mock_ks.is_halted.return_value = True

    eng = build_engine(config, conn=conn, pit=pit, clock=clock, kill_switch=mock_ks)

    result = eng.run_cycle(as_of=_AS_OF)

    # Kill switch halted → no orders, ideas_processed == 0
    assert result.orders_submitted == 0, (
        f"Expected 0 orders when kill switch halted, got {result.orders_submitted}"
    )
    assert result.ideas_processed == 0, (
        f"Expected ideas_processed=0 when kill switched halted, got {result.ideas_processed}"
    )
    # Verify kill switch was actually consulted
    mock_ks.is_halted.assert_called_once()

    # Verify no order rows in DB
    rows = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert rows == 0, f"Expected 0 order rows after kill switch halt, got {rows}"


# ---------------------------------------------------------------------------
# Test 9 (Finding 2) — 0 live advisors → quorum HALTED → no orders
# ---------------------------------------------------------------------------

def test_zero_live_advisors_halts_cycle(tmp_path: Path):
    """Zero live advisors (all returning None) must produce no orders.

    Finding 2 fix: the engine now counts ACTUAL live opinions from THIS
    cycle instead of always passing len(advisor_map)=2.
    """
    import dataclasses

    db_path = str(tmp_path / "quorum_test.db")
    audit_path = str(tmp_path / "audit.jsonl")

    config = load_config()
    config = dataclasses.replace(
        config,
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=audit_path,
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )

    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())

    # Seed filings so there would normally be signals.
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker="AAPL", n_buyers=3)
    pit = _build_pit_with_price("AAPL")

    eng = build_engine(config, conn=conn, pit=pit, clock=clock)

    # Replace advisor map with advisors that ALL return None (abstain).
    eng.advisor_map = {
        "A1.insider": lambda: None,
        "A1.congress": lambda: None,
    }

    result = eng.run_cycle(as_of=_AS_OF)

    # 0 live advisors → engine halts before gathering → no orders
    assert result.orders_submitted == 0, (
        f"Expected 0 orders with 0 live advisors, got {result.orders_submitted}"
    )
    rows = conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
    assert rows == 0, f"Expected 0 DB rows with 0 live advisors, got {rows}"


# ---------------------------------------------------------------------------
# Test 10 (Finding 3) — orders use real price, not $1.00 stub
# ---------------------------------------------------------------------------

def test_orders_use_real_price_from_pit(engine_and_conn):
    """The fill price must come from PITGateway.get('price_open'), not from a $1.00 stub.

    Finding 3 fix: the engine now fetches price_open from PITGateway and passes
    it to submit_order as raw_price.  We verify the fill price is NOT near $1.
    """
    eng, conn, config, audit_path = engine_and_conn

    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted >= 1, (
        f"Expected >=1 orders for price test, got {result.orders_submitted}"
    )

    positions = eng.executor.get_positions()
    assert len(positions) >= 1, "Expected open positions after cycle"

    for ticker, pos in positions.items():
        # price_open seeded at 150.0 in fixture; slippage-adjusted price must be ~150
        assert pos.avg_price > 1.0, (
            f"Fill price for {ticker} is {pos.avg_price} — looks like the $1.00 stub "
            f"is still being used (Finding 3 not fixed)"
        )
        # Check it's in a realistic band: ~150 * (1 + slippage) ≈ 150.075
        assert 100.0 < pos.avg_price < 200.0, (
            f"Fill price {pos.avg_price} for {ticker} is outside expected range"
        )


# ---------------------------------------------------------------------------
# #5a — opinion persistence + linkage + fallback-rate≈0 (E1/E3)
# ---------------------------------------------------------------------------

class TestOpinionPersistenceAndAttribution:
    def test_cycle_persists_opinion_linked_to_idea(self, engine_and_conn):
        """After a cycle, the non-abstain A1.insider opinion is persisted and
        linked (idea_id) to the AAPL/LONG idea by typed (ticker, bucket) (E3)."""
        from arbiter.signals import opinion_store

        eng, conn, config, audit_path = engine_and_conn
        eng.run_cycle(as_of=_AS_OF)

        # The AAPL idea minted this cycle.
        idea_row = conn.execute(
            "SELECT idea_id FROM ideas WHERE ticker='AAPL' AND is_superseded=0 "
            "ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        assert idea_row is not None
        idea_id = idea_row["idea_id"]

        linked = opinion_store.query_opinions_for_idea(conn, idea_id)
        assert len(linked) >= 1
        assert any(r["advisor_id"] == "A1.insider" for r in linked)
        # PIT-clean: persisted at the decision as_of.
        assert all(r["created_at"] == _AS_OF.isoformat() for r in linked)

    def test_rerun_does_not_duplicate_opinion_rows(self, engine_and_conn):
        """Idempotency: re-running the cycle at the same as_of does not duplicate
        opinion rows (insert-only + SELECT-guard, D1)."""
        eng, conn, config, audit_path = engine_and_conn
        eng.run_cycle(as_of=_AS_OF)
        n1 = conn.execute("SELECT COUNT(*) c FROM opinions").fetchone()["c"]
        eng.run_cycle(as_of=_AS_OF)
        n2 = conn.execute("SELECT COUNT(*) c FROM opinions").fetchone()["c"]
        assert n2 == n1

    def test_no_fallback_proxy_after_opinion_persisting_cycle(self, tmp_path: Path):
        """E1: after a normal cycle that persisted an opinion AND resolved its
        idea, the attribution.fallback_proxy metric is NOT emitted (the resolver
        used the real opinion, not the proxy).  We resolve the idea on a later
        cycle whose clock is past the LONG horizon."""
        import dataclasses
        import json

        db_path = str(tmp_path / "fb.db")
        metrics_path = str(tmp_path / "metrics.jsonl")
        audit_path = str(tmp_path / "audit.jsonl")
        config = dataclasses.replace(
            load_config(), live_trading=False, executor_backend="sim",
            db_path=db_path, audit_path=audit_path, metrics_path=metrics_path,
        )
        conn = get_connection(db_path)
        run_migrations(conn, applied_at=_AS_OF.isoformat())
        _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker="AAPL", n_buyers=3)

        # PIT with prices spanning entry (as_of) through well past the LONG
        # horizon so the outcome sweep can label on the resolution cycle.
        fixture = FixtureSource()
        for off in range(-400, 400):
            d = _AS_OF + timedelta(days=off)
            for t in ("AAPL", "SPY"):
                fixture.add("price_open", t, d, 150.0)
                fixture.add("price_close", t, d, 150.0)
            fixture.add("spread", "AAPL", d, 0.01)
            fixture.add("adv_20d", "AAPL", d, 10_000_000.0)
        pit = PITGateway()
        for k in ("price_open", "price_close", "spread", "adv_20d"):
            pit.register_source(k, fixture)

        clock = BacktestClock(_AS_OF)
        eng = build_engine(config, conn=conn, pit=pit, clock=clock)

        # Entry cycle: persists the opinion + mints the idea (→ MONITORED).
        eng.run_cycle(as_of=_AS_OF)

        # Resolution cycle: clock past the LONG bucket horizon (240d) so the
        # exit monitor fires a horizon-expiry SELL and the close-out resolves the
        # idea via the persisted opinion (NOT the proxy fallback).
        later = _AS_OF + timedelta(days=260)
        eng.run_cycle(as_of=later)

        # An outcome exists, attributed to the real opinion's advisor.
        outs = conn.execute(
            "SELECT advisor_id, stance_score FROM outcomes WHERE is_superseded=0"
        ).fetchall()
        assert len(outs) >= 1
        assert any(o["advisor_id"] == "A1.insider" for o in outs)
        # The real-attribution outcome carries the opinion's (non-zero) stance.
        assert any(abs(o["stance_score"]) > 0.0 for o in outs)

        # fallback_proxy must NOT have fired this run.
        events = []
        with open(metrics_path, encoding="utf-8") as fh:
            for line in fh:
                events.append(json.loads(line)["event"])
        assert "attribution.fallback_proxy" not in events
