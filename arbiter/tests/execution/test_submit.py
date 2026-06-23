"""Tests for arbiter.execution.submit."""
from __future__ import annotations

import sqlite3
from datetime import date

import pytest

from arbiter.data.slippage import model_slippage
from arbiter.execution.submit import (
    submit_order,
    _SKIP_SENTINEL,
    _ZERO_SHARE_SKIP,
    SubmitResult,
)
from arbiter.shared.sim_executor import SimExecutor
from arbiter.types import HorizonBucket, OrderSide

from tests.execution.conftest import make_paper_order


class TestSubmitOrder:
    """submit_order end-to-end tests with SimExecutor."""

    def test_submit_returns_order_id(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Successful submit returns a SubmitResult carrying the order_id.

        qty is a dollar NOTIONAL (spec A0); $5,000 / ~$100 → 49 whole shares.
        """
        order = make_paper_order(qty=5_000.0)
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )
        assert isinstance(result, SubmitResult)
        assert result.order_id == order.order_id
        assert result.status == "filled"
        assert result.filled is True
        # Ledger records SHARES, not notional.
        row = mem_conn.execute(
            "SELECT qty FROM orders WHERE order_id = ?", (order.order_id,)
        ).fetchone()
        assert row["qty"] == 49.0

    def test_submit_uses_slippage_adjusted_price(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """SimExecutor fills at slippage-adjusted price (model_slippage applied).

        Finding 3 fix: raw_price must now be passed explicitly by the caller.
        The $1.00 stub fallback has been removed.
        """
        spread = 0.10
        raw_price = 150.0  # real entry price passed by caller (e.g. from PITGateway)
        order = make_paper_order(qty=5_000.0)  # $5k notional → ~33 shares
        expected_limit = model_slippage(raw_price, spread)

        submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=spread,
            raw_price=raw_price,
            audit_path=str(tmp_audit),
        )

        # Check the position was filled at the slippage-adjusted price
        positions = sim_executor.get_positions()
        assert order.ticker in positions
        assert abs(positions[order.ticker].avg_price - expected_limit) < 1e-9

    def test_resubmit_same_order_is_noop(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Submitting the same order twice returns DUPLICATE_SKIP on the second call."""
        order = make_paper_order(qty=5_000.0)
        result1 = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )
        assert result1.order_id == order.order_id
        assert result1.duplicate is False

        result2 = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )
        assert result2.status == _SKIP_SENTINEL
        assert result2.duplicate is True
        assert result2.order_id is None

    def test_dedup_hash_unique_constraint_in_db(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """The orders table enforces UNIQUE on dedup_hash."""
        order = make_paper_order(qty=5_000.0)
        dh = order.dedup_hash

        # Insert manually with the same dedup_hash
        mem_conn.execute(
            """
            INSERT INTO orders
                (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
                 entry_date, advisor_signature, exits_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "other-id",
                dh,
                order.ticker,
                order.side.value,
                order.qty,
                order.horizon_bucket.value,
                str(order.entry_date),
                order.advisor_signature,
                "{}",
                "filled",
                "2024-01-15T12:00:00+00:00",
            ),
        )
        mem_conn.commit()

        # submit_order should detect this and return SKIP
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )
        assert result.status == _SKIP_SENTINEL
        assert result.duplicate is True

    def test_audit_entry_written_on_submit(
        self, mem_conn, sim_executor, fixed_clock, tmp_path
    ):
        """An audit entry is written for every successful submission."""
        import json
        audit_path = str(tmp_path / "audit.jsonl")
        order = make_paper_order(qty=5_000.0)

        submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=audit_path,
        )

        lines = open(audit_path).readlines()
        events = [json.loads(line)["event"] for line in lines if line.strip()]
        assert "order.submitted" in events

    def test_order_persisted_in_db(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Submitted order appears in the orders table."""
        order = make_paper_order(qty=5_000.0)
        submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )

        row = mem_conn.execute(
            "SELECT * FROM orders WHERE order_id = ?",
            (order.order_id,),
        ).fetchone()
        assert row is not None
        assert row["ticker"] == order.ticker
        assert row["side"] == order.side.value

    def test_zero_share_skip(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A notional that floors to 0 shares is skipped — no place, no persist (A0)."""
        # $50 notional at ~$100/share → 0 whole shares.
        order = make_paper_order(qty=50.0)
        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.05,
            raw_price=100.0,
            audit_path=str(tmp_audit),
        )
        assert result.status == _ZERO_SHARE_SKIP
        assert result.zero_share is True
        assert result.order_id is None
        # No order row persisted, no position taken.
        row = mem_conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order.order_id,)
        ).fetchone()
        assert row is None
        assert sim_executor.get_positions() == {}


class TestBrokerRejectionBreaker:
    """Finding 4: broker rejection trips circuit breaker and raises BrokerError."""

    def test_broker_rejection_trips_breaker(self, mem_conn, fixed_clock, tmp_audit):
        """When AlpacaAdapter rejects an order, broker_non_200 breaker is tripped."""
        from unittest.mock import MagicMock
        from arbiter.execution.alpaca_adapter import AlpacaAdapter, BrokerError
        from arbiter.safety.breakers import CircuitBreaker
        from arbiter.config import Config

        cfg = Config(
            live_trading=True,
            executor_backend="sim",
            db_path=":memory:",
            audit_path="/tmp/audit.jsonl",
            metrics_path="/tmp/metrics.jsonl",
            max_position_pct=0.05,
            max_sector_pct=0.20,
            max_gross_pct=0.80,
            max_open_positions=20,
            adv_cap_pct=0.02,
            alpaca_api_key="key123",
            alpaca_secret_key="secret123",
            alpaca_paper_base_url="https://paper-api.alpaca.markets",
            alpaca_data_base_url="https://data.alpaca.markets",
            alpaca_timeout=20.0,
            edgar_user_agent="test@test.com",
            kill_switch_url="",
            alert_webhook_url="",
        )

        # AlpacaAdapter that always rejects (http_post always raises)
        def failing_post(url, headers, json_body):
            raise RuntimeError("HTTP 503")

        executor = AlpacaAdapter(config=cfg, http_post=failing_post)
        breaker = CircuitBreaker()

        order = make_paper_order(qty=5_000.0)

        # submit_order with a failing AlpacaAdapter must trip the breaker
        with pytest.raises(BrokerError):
            submit_order(
                order,
                executor,
                fixed_clock,
                conn=mem_conn,
                spread=0.01,
                raw_price=150.0,
                breaker=breaker,
                audit_path=str(tmp_audit),
            )

        # Breaker must now be latched
        tripped = breaker.any_tripped(mem_conn)
        assert "broker_non_200" in tripped, (
            f"Expected broker_non_200 tripped, got {tripped} (Finding 4)"
        )

    def test_sim_executor_never_trips_breaker(self, mem_conn, sim_executor, fixed_clock, tmp_audit):
        """SimExecutor never rejects, so no breaker is tripped."""
        from arbiter.safety.breakers import CircuitBreaker

        breaker = CircuitBreaker()
        order = make_paper_order(qty=5_000.0)

        result = submit_order(
            order,
            sim_executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.01,
            raw_price=150.0,
            breaker=breaker,
            audit_path=str(tmp_audit),
        )

        assert result.order_id == order.order_id
        tripped = breaker.any_tripped(mem_conn)
        assert tripped == [], f"SimExecutor should not trip any breaker, got {tripped}"


class TestExecutorSelection:
    """executor_backend selects the broker (spec §4.1); live_trading is not consulted."""

    @staticmethod
    def _cfg(*, executor_backend="sim", api_key="key123", secret_key="secret123"):
        from arbiter.config import Config

        return Config(
            live_trading=False,
            executor_backend=executor_backend,
            db_path=":memory:",
            audit_path="/tmp/audit.jsonl",
            metrics_path="/tmp/metrics.jsonl",
            max_position_pct=0.05,
            max_sector_pct=0.20,
            max_gross_pct=0.80,
            max_open_positions=20,
            adv_cap_pct=0.02,
            alpaca_api_key=api_key,
            alpaca_secret_key=secret_key,
            alpaca_paper_base_url="https://paper-api.alpaca.markets",
            alpaca_data_base_url="https://data.alpaca.markets",
            alpaca_timeout=20.0,
            edgar_user_agent="test@test.com",
            kill_switch_url="",
            alert_webhook_url="",
        )

    def test_build_executor_default_is_sim(self):
        """executor_backend=sim → SimExecutor even with keys present."""
        from arbiter.execution.alpaca_adapter import build_executor
        from arbiter.shared.sim_executor import SimExecutor

        executor = build_executor(self._cfg(executor_backend="sim"))
        assert isinstance(executor, SimExecutor)

    def test_build_executor_alpaca_paper_with_keys_is_alpaca(self):
        """executor_backend=alpaca_paper + keys → AlpacaAdapter (paper endpoint)."""
        from arbiter.execution.alpaca_adapter import build_executor, AlpacaAdapter

        executor = build_executor(self._cfg(executor_backend="alpaca_paper"))
        assert isinstance(executor, AlpacaAdapter)

    def test_build_executor_alpaca_paper_no_keys_is_sim(self):
        """executor_backend=alpaca_paper but missing keys → fail-closed to SimExecutor."""
        from arbiter.execution.alpaca_adapter import build_executor
        from arbiter.shared.sim_executor import SimExecutor

        executor = build_executor(
            self._cfg(executor_backend="alpaca_paper", api_key="", secret_key="")
        )
        assert isinstance(executor, SimExecutor)

    def test_invalid_executor_backend_raises_config_error(self, monkeypatch):
        """An invalid EXECUTOR_BACKEND value raises ConfigError (fail-closed)."""
        from arbiter.config import ConfigError, load_config

        monkeypatch.setenv("EXECUTOR_BACKEND", "live_real_money")
        with pytest.raises(ConfigError):
            load_config()


class TestSubmitOrderExitPath:
    """B3 — presized_shares (skip A0 divide) + is_exit (local-ledger-only dedup)."""

    def test_presized_shares_skips_notional_divide(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A SELL sized in SHARES is NOT re-divided by price (A0 bypass)."""
        # Seed a held position so the SELL can fill.
        from arbiter.shared.executor import OrderIntent
        from arbiter.db.helpers import generate_ulid
        sim_executor.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY,
                                       qty=10.0, limit_price=300.0))
        order = make_paper_order(ticker="AAPL", side=OrderSide.SELL, qty=10.0,
                                 advisor_sig="A1.insider:exit")
        result = submit_order(
            order, sim_executor, fixed_clock, conn=mem_conn, spread=0.01,
            raw_price=300.0, audit_path=str(tmp_audit),
            presized_shares=10, is_exit=True,
        )
        assert result.status == "filled"
        # Ledger qty == 10 shares (NOT 10/price ≈ 0).
        row = mem_conn.execute("SELECT qty FROM orders WHERE order_id=?",
                               (order.order_id,)).fetchone()
        assert row["qty"] == 10.0
        # Position fully closed.
        assert "AAPL" not in sim_executor.get_positions()

    def test_is_exit_not_blocked_by_position_presence(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A held position must NOT block its own exit SELL (broker check skipped)."""
        from arbiter.shared.executor import OrderIntent
        from arbiter.db.helpers import generate_ulid
        sim_executor.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY,
                                       qty=5.0, limit_price=100.0))
        order = make_paper_order(ticker="AAPL", side=OrderSide.SELL, qty=5.0,
                                 advisor_sig="A1.insider:exit")
        result = submit_order(
            order, sim_executor, fixed_clock, conn=mem_conn, spread=0.01,
            raw_price=100.0, audit_path=str(tmp_audit),
            presized_shares=5, is_exit=True,
        )
        assert result.duplicate is False
        assert result.status == "filled"

    def test_second_identical_exit_sell_is_idempotent(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A repeated identical SELL (same dedup_hash) is blocked by the local ledger."""
        from arbiter.shared.executor import OrderIntent
        from arbiter.db.helpers import generate_ulid
        sim_executor.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY,
                                       qty=10.0, limit_price=100.0))
        o1 = make_paper_order(ticker="AAPL", side=OrderSide.SELL, qty=10.0,
                              advisor_sig="A1.insider:exit")
        r1 = submit_order(o1, sim_executor, fixed_clock, conn=mem_conn, spread=0.01,
                          raw_price=100.0, audit_path=str(tmp_audit),
                          presized_shares=10, is_exit=True)
        assert r1.status == "filled"
        # Re-buy and attempt the SAME logical SELL (same dedup_hash) → blocked.
        sim_executor.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY,
                                       qty=10.0, limit_price=100.0))
        o2 = make_paper_order(ticker="AAPL", side=OrderSide.SELL, qty=10.0,
                              advisor_sig="A1.insider:exit")  # identical dedup fields
        r2 = submit_order(o2, sim_executor, fixed_clock, conn=mem_conn, spread=0.01,
                          raw_price=100.0, audit_path=str(tmp_audit),
                          presized_shares=10, is_exit=True)
        assert r2.duplicate is True

    def test_sell_slippage_biases_limit_down(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A SELL limit is biased DOWN (B1) so it stays marketable."""
        from arbiter.shared.executor import OrderIntent
        from arbiter.db.helpers import generate_ulid
        sim_executor.place(OrderIntent(generate_ulid(), "AAPL", OrderSide.BUY,
                                       qty=10.0, limit_price=300.0))
        order = make_paper_order(ticker="AAPL", side=OrderSide.SELL, qty=10.0,
                                 advisor_sig="A1.insider:exit")
        submit_order(order, sim_executor, fixed_clock, conn=mem_conn, spread=0.10,
                     raw_price=280.0, audit_path=str(tmp_audit),
                     presized_shares=10, is_exit=True)
        sell_report = [r for r in sim_executor._reports if r.side == OrderSide.SELL][-1]
        # 280*(1-0.0005) - 0.5*0.10 < 280
        assert sell_report.avg_fill_price < 280.0


class _ScriptedExecutor:
    """Minimal executor returning a pre-built ExecutionReport for ``place``.

    Lets tests drive ``rejected`` / ``partial`` outcomes that SimExecutor never
    produces, without a broker.  ``get_positions`` is empty so entry BUYs are
    not blocked by the position-presence dedup check.
    """

    name = "scripted"

    def __init__(self, report_factory):
        self._report_factory = report_factory

    def place(self, intent):
        return self._report_factory(intent)

    def get_positions(self):
        return {}


def _make_report(intent, *, status, filled_qty, reject_reason=""):
    from arbiter.shared.executor import ExecutionReport

    return ExecutionReport(
        order_id=intent.order_id,
        ticker=intent.ticker,
        side=intent.side,
        status=status,
        filled_qty=filled_qty,
        avg_fill_price=intent.limit_price,
        gross_notional=filled_qty * (intent.limit_price or 0.0),
        realized_pl=None,
        reject_reason=reject_reason,
        executor="scripted",
        paper_only=True,
    )


class TestRejectedNeverPersists:
    """D1 P1 — a ``rejected`` report NEVER persists an order row, breaker or not."""

    def test_rejected_with_no_breaker_does_not_persist(
        self, mem_conn, fixed_clock, tmp_audit
    ):
        """A rejected order with breaker=None must NOT poison the dedup slot."""
        from arbiter.execution.alpaca_adapter import BrokerError

        executor = _ScriptedExecutor(
            lambda intent: _make_report(
                intent, status="rejected", filled_qty=0.0, reject_reason="no buying power"
            )
        )
        order = make_paper_order(qty=5_000.0)

        with pytest.raises(BrokerError):
            submit_order(
                order,
                executor,
                fixed_clock,
                conn=mem_conn,
                spread=0.01,
                raw_price=100.0,
                breaker=None,  # the crux: no breaker present
                audit_path=str(tmp_audit),
            )

        # No order row persisted → the dedup slot is free for a legit retry.
        row = mem_conn.execute(
            "SELECT 1 FROM orders WHERE dedup_hash = ?", (order.dedup_hash,)
        ).fetchone()
        assert row is None, "rejected order must NOT persist (would poison dedup)"


class TestPartialPersistsFilledQty:
    """D4 P2 — a ``partial`` fill persists filled_qty, not the requested qty."""

    def test_partial_sell_persists_filled_qty(
        self, mem_conn, fixed_clock, tmp_audit
    ):
        """A partial SELL of 10 requested / 4 filled persists qty=4 in the ledger."""
        executor = _ScriptedExecutor(
            lambda intent: _make_report(intent, status="partial", filled_qty=4.0)
        )
        order = make_paper_order(
            ticker="AAPL", side=OrderSide.SELL, qty=10.0, advisor_sig="A1.insider:exit"
        )
        result = submit_order(
            order,
            executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.01,
            raw_price=100.0,
            audit_path=str(tmp_audit),
            presized_shares=10,  # requested 10 shares
            is_exit=True,
        )
        assert result.status == "partial"
        row = mem_conn.execute(
            "SELECT qty FROM orders WHERE order_id = ?", (order.order_id,)
        ).fetchone()
        assert row["qty"] == 4.0, "partial must persist filled_qty (4), not requested (10)"


# ---------------------------------------------------------------------------
# Wave 2 — SubmitResult.filled_notional (realized notional fold)
# ---------------------------------------------------------------------------

from arbiter.shared.executor import (
    Executor as _Executor,
    ExecutionReport as _ExecReport,
    OrderIntent as _OrderIntent,
)


class _PartialExecutor(_Executor):
    """Stub executor that reports a configurable partial/full fill."""

    name = "partial_stub"

    def __init__(self, *, status: str, filled_qty: float, avg_fill_price: float | None):
        self._status = status
        self._filled_qty = filled_qty
        self._avg_fill_price = avg_fill_price

    def place(self, intent: _OrderIntent) -> _ExecReport:
        return _ExecReport(
            order_id=intent.order_id,
            ticker=intent.ticker,
            side=intent.side,
            status=self._status,
            filled_qty=self._filled_qty,
            avg_fill_price=self._avg_fill_price,
            gross_notional=(self._avg_fill_price or 0.0) * self._filled_qty,
            realized_pl=None,
            reject_reason="",
            executor=self.name,
            paper_only=True,
        )

    def cancel(self, order_id):  # pragma: no cover - unused
        raise NotImplementedError

    def get_positions(self):
        return {}

    def get_account(self):  # pragma: no cover - unused
        raise NotImplementedError


class TestFilledNotional:
    def test_submit_result_filled_notional_on_full(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A full SimExecutor fill exposes filled_notional ≈ avg_fill_price × filled_qty."""
        order = make_paper_order(qty=5_000.0)
        result = submit_order(
            order, sim_executor, fixed_clock, conn=mem_conn,
            spread=0.05, raw_price=100.0, audit_path=str(tmp_audit),
        )
        assert result.status == "filled"
        assert result.avg_fill_price is not None
        # 49 whole shares filled at the slippage-adjusted price.
        assert result.filled_notional == pytest.approx(result.avg_fill_price * 49.0)

    def test_submit_result_filled_notional_partial(
        self, mem_conn, fixed_clock, tmp_audit
    ):
        """A partial fill yields filled_notional reflecting the PARTIAL shares."""
        # Requested $5,000 / $100 → 49 shares; broker fills only 20.
        execu = _PartialExecutor(status="partial", filled_qty=20.0, avg_fill_price=100.0)
        order = make_paper_order(qty=5_000.0)
        result = submit_order(
            order, execu, fixed_clock, conn=mem_conn,
            spread=0.0, raw_price=100.0, audit_path=str(tmp_audit),
        )
        assert result.status == "partial"
        assert result.filled_notional == pytest.approx(2_000.0)  # 20 × 100
        # < requested notional of ~4,900 (49 × 100)
        assert result.filled_notional < 49.0 * 100.0

    def test_submit_result_filled_notional_none_when_skipped(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """A zero-share skip leaves filled_notional None (nothing placed)."""
        order = make_paper_order(qty=1.0)  # $1 notional → 0 shares
        result = submit_order(
            order, sim_executor, fixed_clock, conn=mem_conn,
            spread=0.0, raw_price=100.0, audit_path=str(tmp_audit),
        )
        assert result.order_id is None
        assert result.filled_notional is None
