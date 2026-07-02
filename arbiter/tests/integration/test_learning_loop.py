"""Engine learning-loop integration tests (sub-project #4).

Offline: temp SQLite + BacktestClock + FixtureSource, synthetic ResolvedOutcomes.

Covers:
  - T1 bootstrap: zero/cold outcomes still trade (non-empty pool, FusionOutput,
    no skipped bucket).
  - T2 dormant ledger (<60) still floors.
  - T3 graduation: ≥40 non-abstain positive-alpha insider gets a learned weight
    that replaces the floor and shifts the pool.
  - T5 negative-skill: a consistently-wrong advisor is suppressed (0/shadow) and
    excluded from the pool while a cold sibling keeps trading.
  - D2: a backtest recomputes per step (no stale cross-step cache).
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.config import load_config
from arbiter.data.clock import Clock
from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import ResolvedOutcome
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.evaluation.outcome_store import store_outcome
from arbiter.fusion.engine import fuse
from arbiter.types import ConfidenceSource, HorizonBucket

_UTC = timezone.utc
T = datetime(2025, 6, 1, 12, 0, 0, tzinfo=_UTC)
INSIDER = "A1.insider"
CONGRESS = "A1.congress"
ACTIVIST = "A1.activist"
FUND = "A1.fund"
INSIDER_SELL = "A1.insider_sell"
CONGRESS_SELL = "A1.congress_sell"


def _engine(conn: sqlite3.Connection, as_of: datetime, *, equal_floor: float | None = None):
    clock = BacktestClock(as_of)
    pit = PITGateway()
    fixture = FixtureSource()
    pit.register_source("price_open", fixture)
    cfg = load_config()
    if equal_floor is not None:
        cfg = dataclasses.replace(cfg, trust_equal_floor=equal_floor)
    return build_engine(cfg, conn=conn, pit=pit, clock=clock)


def _conn():
    c = get_connection(":memory:")
    run_migrations(c)
    return c


def _store(conn, idea_id, advisor_id, *, binary, alpha, created_at, conf=0.8, horizon=90):
    o = ResolvedOutcome(
        idea_id=idea_id,
        advisor_id=advisor_id,
        ticker="AAA",
        alpha_bps=alpha,
        binary=binary,
        advisor_confidence=conf,
        stance_score=float(binary),
        abstained=False,
        horizon_days=horizon,
        label_kind="normal",
    )
    store_outcome(o, conn, as_of=created_at)


def _opinions():
    op_i = Opinion(
        advisor_id=INSIDER, ticker="AAA", stance_score=0.6, confidence=0.8,
        confidence_source=ConfidenceSource.MODELED, horizon_days=180, as_of=T,
        rationale="r", source_fingerprint="fp-i", run_group_id="rg",
    )
    op_c = Opinion(
        advisor_id=CONGRESS, ticker="AAA", stance_score=0.6, confidence=0.8,
        confidence_source=ConfidenceSource.MODELED, horizon_days=180, as_of=T,
        rationale="r", source_fingerprint="fp-c", run_group_id="rg",
    )
    return [op_i, op_c]


def test_T1_bootstrap_zero_outcomes_still_trades():
    conn = _conn()
    eng = _engine(conn, T)
    wb, cal = eng._build_learning_inputs(T)

    # all registered A1 advisors present, floored, non-shadow
    assert set(wb.weights.keys()) == {INSIDER, CONGRESS, ACTIVIST, FUND, INSIDER_SELL, CONGRESS_SELL}
    for aw in wb.weights.values():
        assert aw.shadow is False
        assert aw.weight > 0.0
    assert bool(cal.is_cold_start) is True

    # fuse must NOT skip the bucket
    out = fuse(_opinions(), wb, cal)
    assert out  # non-empty
    bucket = HorizonBucket.LONG
    assert bucket in out
    assert out[bucket].n_opinions == 2


def test_T2_dormant_ledger_still_floors():
    conn = _conn()
    # 40 outcomes (< 60 activation) → ledger dormant / returns None
    for i in range(40):
        _store(conn, f"i{i}", CONGRESS, binary=1, alpha=50.0,
               created_at=T - timedelta(days=30 + i))
    eng = _engine(conn, T)
    wb, _cal = eng._build_learning_inputs(T)
    # still floored (dormant) — congress floored, insider (no outcomes) floored
    assert wb.weights[CONGRESS].weight == pytest.approx(eng.config.trust_equal_floor)
    assert wb.weights[CONGRESS].shadow is False


def test_T3_graduation_shifts_pool():
    conn = _conn()
    base = T - timedelta(days=10)
    # insider: 45 strongly-positive non-abstain outcomes → past the 30+10 ramp,
    # high BSS → learned weight at the ceiling (0.50).
    for i in range(45):
        _store(conn, f"ins{i}", INSIDER, binary=1, alpha=120.0,
               created_at=base - timedelta(hours=i))
    # congress: only 20 → stays in shadow (floored)
    for i in range(20):
        _store(conn, f"con{i}", CONGRESS, binary=1, alpha=80.0,
               created_at=base - timedelta(hours=i))

    # Probationary floor 0.3 < the graduated ceiling 0.5 so graduation is visible.
    eng = _engine(conn, T, equal_floor=0.3)
    wb, cal = eng._build_learning_inputs(T)

    ins = wb.weights[INSIDER]
    con = wb.weights[CONGRESS]
    floor = eng.config.trust_equal_floor
    # insider graduated → learned weight in (0, 0.50], replacing the floor.
    assert ins.shadow is False
    assert 0.0 < ins.weight <= 0.50
    assert ins.weight != pytest.approx(floor)  # learned, not the floor
    # congress still cold → floored
    assert con.weight == pytest.approx(floor)

    # Pool reflects the blend: graduated high-skill insider (0.50) up-weighted vs
    # cold congress floored (0.30) → insider's normalised share > congress.
    out = fuse(_opinions(), wb, cal)
    bucket = HorizonBucket.LONG
    contribs = out[bucket].advisor_contributions
    assert contribs[INSIDER] > contribs[CONGRESS]


def test_T5_negative_skill_suppressed_cold_sibling_trades(monkeypatch):
    """A consistently-wrong advisor is suppressed (0/shadow) and excluded, while a
    cold sibling keeps trading.

    NOTE: the ResolvedOutcome encoding ties the reconstructed forecast direction to
    the outcome direction (``p_hat = stance_to_prob(binary * confidence)``), so a
    NEGATIVE BSS is structurally unreachable from synthetic outcome rows alone (the
    ledger's own test acknowledges this and exercises the cap via ``_apply_caps``).
    We therefore force ``brier_skill_score`` negative for the insider so the LEDGER
    genuinely emits ``cap_reason="negative_skill"`` and we can prove the engine
    suppresses it end-to-end.
    """
    import arbiter.trust.ledger as ledger_mod

    conn = _conn()
    base = T - timedelta(days=10)
    # insider: 60 non-abstain outcomes → crosses the activation threshold so the
    # ledger emits a (suppressed) weight; congress stays cold (no outcomes) → floored.
    for i in range(60):
        _store(conn, f"ins{i}", INSIDER, binary=1, alpha=50.0,
               created_at=base - timedelta(hours=i))
    # congress: cold (no outcomes) → floored

    _real_bss = ledger_mod.brier_skill_score

    def _bss(outcomes, dates, as_of):
        if outcomes and outcomes[0].advisor_id == INSIDER:
            return -0.5  # sub-chance → negative skill
        return _real_bss(outcomes, dates, as_of)

    monkeypatch.setattr(ledger_mod, "brier_skill_score", _bss)

    eng = _engine(conn, T, equal_floor=0.3)
    wb, cal = eng._build_learning_inputs(T)

    assert wb.weights[INSIDER].weight == 0.0
    assert wb.weights[INSIDER].shadow is True
    assert wb.weights[CONGRESS].weight == pytest.approx(eng.config.trust_equal_floor)
    assert wb.weights[CONGRESS].shadow is False

    # fuse: insider excluded, congress still pools → bucket trades (no deadlock).
    out = fuse(_opinions(), wb, cal)
    bucket = HorizonBucket.LONG
    assert bucket in out
    assert INSIDER not in out[bucket].advisor_contributions
    assert CONGRESS in out[bucket].advisor_contributions


def test_D2_backtest_recomputes_each_step_no_stale_cache():
    """A backtest recomputes per step: advancing the clock with NEW outcomes
    changes the resolved bundle (no cross-step cache carrying old decay)."""
    conn = _conn()
    base = T - timedelta(days=20)
    # Seed enough for graduation by step 2; high skill → learned weight at the
    # ceiling (0.50), distinct from the 0.3 probationary floor.
    for i in range(45):
        _store(conn, f"ins{i}", INSIDER, binary=1, alpha=120.0,
               created_at=base - timedelta(hours=i))

    eng = _engine(conn, T, equal_floor=0.3)

    # Step 1 as_of = base + 1d (outcomes < activation visible? they are 45 < 60,
    # so dormant → floored).
    step1 = base + timedelta(days=1)
    eng.clock.set_as_of(step1)
    wb1, _ = eng._build_learning_inputs(step1)
    assert wb1.weights[INSIDER].weight == pytest.approx(eng.config.trust_equal_floor)

    # Add congress outcomes to cross the 60 activation threshold by step 2.
    for i in range(25):
        _store(conn, f"con{i}", CONGRESS, binary=1, alpha=90.0,
               created_at=base + timedelta(hours=i))

    step2 = T
    eng.clock.set_as_of(step2)
    wb2, _ = eng._build_learning_inputs(step2)
    # Now ≥60 total and insider past ramp → its weight is the learned value,
    # which must DIFFER from step1's floor (proves per-step recompute, no cache).
    assert wb2.weights[INSIDER].weight != pytest.approx(eng.config.trust_equal_floor)
    assert eng._learning_cache is None  # backtest never populates the cache


def test_T6_live_cadence_caches_and_no_duplicate_persist():
    """LIVE mode: two consecutive cycles with NO new outcomes → ledger.update is
    called at most once (cache reused), and trust_weights gets no duplicate insert."""
    conn = _conn()
    base = T - timedelta(days=10)
    # 70 outcomes, enough to activate + fire should_update.
    for i in range(40):
        _store(conn, f"ins{i}", INSIDER, binary=1, alpha=120.0,
               created_at=base - timedelta(hours=i))
    for i in range(30):
        _store(conn, f"con{i}", CONGRESS, binary=1, alpha=80.0,
               created_at=base - timedelta(hours=i))

    # LIVE clock (not BacktestClock) → caching path active.
    pit = PITGateway()
    pit.register_source("price_open", FixtureSource())
    cfg = dataclasses.replace(load_config(), trust_equal_floor=0.3)
    eng = build_engine(cfg, conn=conn, pit=pit, clock=Clock())

    # First cycle: should_update fires → update + persist.
    wb1, _ = eng._build_learning_inputs(T)
    n_rows_1 = conn.execute("SELECT COUNT(*) AS c FROM trust_weights").fetchone()["c"]
    assert eng._learning_cache is not None
    last_update_1 = eng.ledger.last_update_at

    # Second cycle, SAME outcomes (no new) → should_update False → cache reused,
    # ledger NOT updated again, NO new trust_weights rows.
    wb2, _ = eng._build_learning_inputs(T)
    n_rows_2 = conn.execute("SELECT COUNT(*) AS c FROM trust_weights").fetchone()["c"]
    assert eng.ledger.last_update_at == last_update_1  # update not re-run
    assert n_rows_2 == n_rows_1  # no duplicate persist
    # cached bundle reused → same resolved weights
    assert {k: v.weight for k, v in wb2.weights.items()} == {
        k: v.weight for k, v in wb1.weights.items()
    }
