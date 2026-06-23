"""Tests for arbiter.trust.store — PIT cutoff + persistence + warm-start (#4).

Covers:
  - T4 / D0: STRICT created_at < as_of cutoff (same-cycle outcome excluded).
  - T8 / D7: trust_weights round-trip + warm-start.
  - D4: backtest warm-start uses the as_of window, not is_superseded.
  - cap_reason persisted + read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from arbiter.contract.seams import AdvisorWeight, ResolvedOutcome, WeightBundle
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.evaluation.outcome_store import store_outcome, query_outcomes
from arbiter.trust import store as trust_store

_UTC = timezone.utc
T = datetime(2025, 6, 1, 12, 0, 0, tzinfo=_UTC)


@pytest.fixture()
def conn():
    c = get_connection(":memory:")
    run_migrations(c)
    return c


def _outcome(idea_id: str, advisor_id: str, binary: int = 1) -> ResolvedOutcome:
    return ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker="AAA",
        alpha_bps=50.0,
        binary=binary,
        advisor_confidence=0.8,
        stance_score=float(binary),
        abstained=False,
        horizon_days=90,
        label_kind="normal",
    )


def test_strict_cutoff_excludes_same_cycle_outcome(conn):
    """D0: an outcome stamped at created_at == now is NOT in the learning inputs
    (mirrors an exit-monitor/reconcile/sweep row written in the same cycle)."""
    # resolved before T
    store_outcome(_outcome("i-prior", "A1.congress"), conn, as_of=T - timedelta(days=10))
    # resolved exactly AT T (same-cycle writer)
    store_outcome(_outcome("i-now", "A1.congress"), conn, as_of=T)

    by_advisor = trust_store.load_outcomes_for_learning(conn, T)
    ids = [o.idea_id for o, _ in by_advisor.get("A1.congress", [])]
    assert "i-prior" in ids
    assert "i-now" not in ids  # strict < cutoff excludes same-cycle row


def test_query_outcomes_strict_lt_kwarg(conn):
    store_outcome(_outcome("a", "A1.congress"), conn, as_of=T - timedelta(days=1))
    store_outcome(_outcome("b", "A1.congress"), conn, as_of=T)
    le = query_outcomes(conn, as_of=T)  # <= T
    lt = query_outcomes(conn, as_of=T, strict_lt=True)  # < T
    assert {r["idea_id"] for r in le} == {"a", "b"}
    assert {r["idea_id"] for r in lt} == {"a"}


def test_persist_and_warm_start_round_trip(conn):
    """T8: persisted learned weights reconstruct via load_latest_weight_bundle."""
    bundle = WeightBundle(
        weights={
            "A1.insider": AdvisorWeight("A1.insider", 0.42, 0.34, 0.50, shadow=False),
            "A1.congress": AdvisorWeight("A1.congress", 0.0, 0.0, 0.0, shadow=True),
        },
        correlation_matrix={},
    )
    trust_store.persist_weight_bundle(
        conn, bundle, as_of=T, cap_reasons={"A1.congress": "negative_skill"}
    )
    lb = trust_store.load_latest_weight_bundle(conn, T, backtest=False)
    assert lb is not None
    assert lb.weights["A1.insider"].weight == pytest.approx(0.42)
    assert lb.weights["A1.congress"].shadow is True
    reasons = trust_store.load_cap_reasons(conn, T, backtest=False)
    assert reasons["A1.congress"] == "negative_skill"
    assert reasons["A1.insider"] is None


def test_persist_supersedes_prior_row(conn):
    """A second persist supersedes the prior live row (no duplicate live rows)."""
    b1 = WeightBundle(
        weights={"A1.insider": AdvisorWeight("A1.insider", 0.30, 0.2, 0.4, shadow=False)},
        correlation_matrix={},
    )
    b2 = WeightBundle(
        weights={"A1.insider": AdvisorWeight("A1.insider", 0.45, 0.3, 0.5, shadow=False)},
        correlation_matrix={},
    )
    trust_store.persist_weight_bundle(conn, b1, as_of=T - timedelta(days=7))
    trust_store.persist_weight_bundle(conn, b2, as_of=T)
    live = conn.execute(
        "SELECT weight FROM trust_weights WHERE advisor_id='A1.insider' AND is_superseded=0"
    ).fetchall()
    assert len(live) == 1
    assert live[0]["weight"] == pytest.approx(0.45)


def test_backtest_warm_start_uses_as_of_window(conn):
    """D4: backtest read keys on as_of <= T, NOT is_superseded (which reflects the
    latest real run, not the replay's point in time)."""
    # Row at T-7 (later superseded), row at T+7 (the "latest real run").
    b_old = WeightBundle(
        weights={"A1.insider": AdvisorWeight("A1.insider", 0.20, 0.1, 0.3, shadow=False)},
        correlation_matrix={},
    )
    b_new = WeightBundle(
        weights={"A1.insider": AdvisorWeight("A1.insider", 0.50, 0.4, 0.5, shadow=False)},
        correlation_matrix={},
    )
    trust_store.persist_weight_bundle(conn, b_old, as_of=T - timedelta(days=7))
    trust_store.persist_weight_bundle(conn, b_new, as_of=T + timedelta(days=7))

    # Live read returns the latest (is_superseded=0) row = the T+7 weight.
    live = trust_store.load_latest_weight_bundle(conn, T, backtest=False)
    assert live.weights["A1.insider"].weight == pytest.approx(0.50)

    # Backtest read AS OF T must NOT see the T+7 row → returns the T-7 weight.
    bt = trust_store.load_latest_weight_bundle(conn, T, backtest=True)
    assert bt.weights["A1.insider"].weight == pytest.approx(0.20)
