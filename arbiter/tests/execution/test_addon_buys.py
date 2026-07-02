"""Tier-2 #5 — add-on buys (pyramiding) across all four layers (2026-07-02).

Spec: docs/specs/2026-07-02-addon-buys-spec.md

- sizing: per-name cap becomes HEADROOM vs already-held notional; the
  open-position-count gate does not apply to an add-on.
- idempotency: ``is_addon=True`` skips ONLY the broker position check; the
  local-ledger dedup still blocks same-day identical re-entry.
- book: ``RiskBook`` exposes ``name_exposure_for`` / feeds decide().
- exit sweep: a full exit closes EVERY MONITORED idea on the ticker.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from arbiter.contract.seams import TradingDecision
from arbiter.execution.exit_monitor import close_all_monitored_for_ticker
from arbiter.execution.idempotency import DuplicateOrderError, ensure_not_duplicate
from arbiter.policy.book import RiskBook
from arbiter.policy.sizing import compute_size
from arbiter.types import DegradationLevel, HorizonBucket, IdeaState, OrderSide

from tests.execution.conftest import make_paper_order
from tests.execution.test_exit_monitor import (
    _AS_OF,
    _advisor_id_for,
    _migrated_conn,
    _pit_with,
    _seed_position_idea_order,
)
from tests.policy.conftest import adv_always, make_fusion

_NOW = datetime(2025, 3, 14, tzinfo=timezone.utc)
_EQUITY = 100_000.0  # name cap at 5% = $5,000


@pytest.fixture()
def cfg():
    """Config with canonical default caps (mirrors tests/policy/conftest.py)."""
    from arbiter.config import Config

    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/dev/null",
        metrics_path="/dev/null",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="",
        alpaca_data_base_url="",
        alpaca_timeout=20.0,
        edgar_user_agent="",
        kill_switch_url="",
        alert_webhook_url="",
    )


def _ok_gate() -> TradingDecision:
    return TradingDecision(
        allowed=True, size_multiplier=1.0,
        level=DegradationLevel.NORMAL, reasons=[],
    )


def _size(cfg, *, name_exposure: float, open_positions: int = 3) -> float:
    return compute_size(
        fusion=make_fusion(conviction=1.0),  # quarter-Kelly $25k → name cap binds
        portfolio_equity=_EQUITY,
        config=cfg,
        gate_decision=_ok_gate(),
        adv_provider=adv_always(100_000_000.0),
        ticker="BAC",
        as_of=_NOW,
        current_open_positions=open_positions,
        current_name_exposure=name_exposure,
    )


class TestSizingNameHeadroom:
    def test_addon_capped_at_name_headroom(self, cfg):
        """Held $3k of the $5k cap → an add sizes to at most $2k."""
        assert _size(cfg, name_exposure=3_000.0) == pytest.approx(2_000.0)

    def test_at_cap_zeroes_addon(self, cfg):
        assert _size(cfg, name_exposure=5_000.0) == 0.0
        assert _size(cfg, name_exposure=6_000.0) == 0.0  # over-cap never negative

    def test_unheld_name_unchanged(self, cfg):
        """Default 0.0 exposure keeps the legacy full name cap."""
        assert _size(cfg, name_exposure=0.0) == pytest.approx(5_000.0)

    def test_count_gate_skipped_for_addon(self, cfg):
        """At max_open_positions an ADD-ON still sizes (no NEW position)."""
        full = cfg.max_open_positions
        assert _size(cfg, name_exposure=3_000.0, open_positions=full) == pytest.approx(2_000.0)
        # ...but a NEW name at capacity stays blocked.
        assert _size(cfg, name_exposure=0.0, open_positions=full) == 0.0


class TestIdempotencyAddonPath:
    def test_addon_skips_broker_check(self, mem_conn, sim_executor, fixed_clock):
        """A held ticker no longer raises when is_addon=True."""
        from arbiter.shared.executor import OrderIntent

        sim_executor.place(
            OrderIntent("01SEED", "BAC", OrderSide.BUY, qty=4.0, limit_price=58.0)
        )
        order = make_paper_order(ticker="BAC", qty=250.0)
        with pytest.raises(DuplicateOrderError):
            ensure_not_duplicate(order, mem_conn, sim_executor)  # legacy path
        ensure_not_duplicate(order, mem_conn, sim_executor, is_addon=True)  # no raise

    def test_local_ledger_still_blocks_addon(
        self, mem_conn, sim_executor, fixed_clock, tmp_audit
    ):
        """Same-day identical order (same dedup hash) stays blocked."""
        from arbiter.execution.submit import submit_order

        first = make_paper_order(ticker="BAC", qty=250.0)
        submit_order(
            first, sim_executor, fixed_clock, conn=mem_conn,
            raw_price=58.0, audit_path=str(tmp_audit),
        )
        dup = make_paper_order(ticker="BAC", qty=250.0)  # identical hash fields
        with pytest.raises(DuplicateOrderError):
            ensure_not_duplicate(dup, mem_conn, sim_executor, is_addon=True)


class TestRiskBookNameExposure:
    def test_name_exposure_reader_and_kwargs(self):
        book = RiskBook({"BAC": 3_000.0, "CVX": 176.0}, sector_for=lambda t: "X")
        assert book.name_exposure_for("BAC") == 3_000.0
        assert book.name_exposure_for("TSLA") == 0.0
        kwargs = book.as_decide_kwargs("BAC")
        assert kwargs["current_name_exposure"] == 3_000.0


class TestFullExitSweep:
    def test_full_exit_closes_all_monitored_ideas_on_ticker(self, tmp_path):
        """Original + add-on ideas BOTH close on one full exit (Tier-2 #5)."""
        from arbiter.shared.sim_executor import SimExecutor

        conn = _migrated_conn(tmp_path)
        ex = SimExecutor(starting_cash=1_000_000.0)
        pit, _fx = _pit_with("AAPL", close=280.0)

        idea1, order1 = _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=10, avg_price=300.0,
            bucket=HorizonBucket.MEDIUM,
            entry_date=_AS_OF.date(), horizon_days=75,
        )
        idea2, _order2 = _seed_position_idea_order(
            conn, ex, ticker="AAPL", shares=5, avg_price=310.0,
            bucket=HorizonBucket.LONG,
            entry_date=_AS_OF.date(), horizon_days=240,
        )

        row = conn.execute(
            "SELECT * FROM orders WHERE order_id = ?", (order1,)
        ).fetchone()
        oid = close_all_monitored_for_ticker(
            conn,
            order_row=row,
            exit_price=290.0,
            exit_as_of=_AS_OF,
            label_kind="early_exit",
            pit=pit,
            advisor_id_for=_advisor_id_for,
        )
        assert oid is not None

        states = {
            r["idea_id"]: r["state"]
            for r in conn.execute("SELECT idea_id, state FROM ideas").fetchall()
        }
        assert states[idea1] == IdeaState.CLOSED.value
        assert states[idea2] == IdeaState.CLOSED.value

        n_outcomes = conn.execute("SELECT COUNT(*) c FROM outcomes").fetchone()["c"]
        assert n_outcomes >= 2  # one per closed idea (at least)
