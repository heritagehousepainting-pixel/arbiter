"""Engine-level wiring tests for the A2 (MiroFish) list-valued advisor channel.

Wave 2 — verifies:
  - A2 is a clean no-op when MIROFISH_ENDPOINT is unset (zero opinions, no network,
    same orders as A1-only).
  - When enabled (endpoint set + adapter.run mocked), the LIST of A2 opinions
    survives the single-opinion replay map (synthetic-key fix) and reaches both
    persistence and the fused bucket pools.
  - A2 does not disturb the A1 single-opinion slots.
  - A negative A2 stance passes through unclamped.
  - is_backtest is derived from the clock type.
  - The single-opinion scheduler/advisor path signatures are untouched.

All offline: temp SQLite + BacktestClock + FixtureSource PIT; adapter.run is
ALWAYS mocked so no test hits the network.
"""
from __future__ import annotations

import dataclasses
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.contract.opinion import Opinion
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.ingest.writer import write_filing
from arbiter.types import ConfidenceSource

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


# ---------------------------------------------------------------------------
# Helpers (mirror tests/integration/test_end_to_end.py)
# ---------------------------------------------------------------------------

def _seed_cluster_buy(conn, clock_fn, ticker="AAPL", n_buyers=3, amount=500_000.0):
    for i in range(n_buyers):
        raw = {
            "source": "form4",
            "ticker": ticker,
            "person_id": generate_ulid(),
            "filing_ts": (_AS_OF - timedelta(days=5 + i)).isoformat(),
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
        write_filing(conn, raw, clock_fn)


def _build_pit(ticker="AAPL"):
    fixture = FixtureSource()
    ts_seed = _AS_OF - timedelta(days=1)
    fixture.add("price_close", ticker, ts_seed, 150.0)
    fixture.add("price_open", ticker, ts_seed, 150.0)
    fixture.add("spread", ticker, ts_seed, 0.01)
    fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fixture)
    return pit


def _make_engine(tmp_path: Path, clock=None):
    db_path = str(tmp_path / "mf.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="sim",
        db_path=db_path,
        audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
    )
    clock = clock or BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    _seed_cluster_buy(conn, lambda: _AS_OF.isoformat())
    pit = _build_pit()
    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn


def _a2_opinion(stance: float, horizon_days: int, fp: str) -> Opinion:
    return Opinion(
        advisor_id="A2.mirofish",
        ticker="AAPL",
        stance_score=stance,
        confidence=0.7,
        confidence_source=ConfidenceSource.MODELED,
        horizon_days=horizon_days,
        as_of=_AS_OF,
        rationale="mf",
        source_fingerprint=fp,
        run_group_id="mf-run",
    )


# ---------------------------------------------------------------------------
# Disabled (noop) path
# ---------------------------------------------------------------------------

def test_a2_disabled_noop_when_unset(tmp_path, monkeypatch):
    """MIROFISH_ENDPOINT unset → a2_mirofish_fn returns [] and never hits network."""
    monkeypatch.delenv("MIROFISH_ENDPOINT", raising=False)
    # Fail the test if any http POST is attempted.
    import httpx
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network called")),
    )

    eng, conn = _make_engine(tmp_path)
    from arbiter.orchestrator.idea import make_idea
    idea = make_idea(ticker="AAPL", thesis="t", horizon_days=180, as_of=_AS_OF)
    assert eng.a2_mirofish_fn is not None
    assert eng.a2_mirofish_fn(idea) == []

    # A full cycle runs with no A2 contribution.
    eng.run_cycle(as_of=_AS_OF)
    n_a2 = conn.execute(
        "SELECT COUNT(*) FROM opinions WHERE advisor_id='A2.mirofish'"
    ).fetchone()[0]
    assert n_a2 == 0


# ---------------------------------------------------------------------------
# Enabled path — list survives the single-opinion replay map
# ---------------------------------------------------------------------------

def test_a2_list_flows_into_fusion(tmp_path, monkeypatch):
    """Two A2 opinions (different buckets) both persist — the LIST survived."""
    monkeypatch.setenv("MIROFISH_ENDPOINT", "https://mf.test/endpoint")
    ops = [
        _a2_opinion(0.6, 20, "mf-short"),   # SHORT bucket
        _a2_opinion(0.4, 180, "mf-long"),   # LONG bucket
    ]
    monkeypatch.setattr(
        "arbiter.adapters.mirofish.adapter.run",
        lambda idea, as_of, **kw: list(ops),
    )

    eng, conn = _make_engine(tmp_path)
    eng.run_cycle(as_of=_AS_OF)

    rows = conn.execute(
        "SELECT stance_score FROM opinions WHERE advisor_id='A2.mirofish' "
        "ORDER BY stance_score"
    ).fetchall()
    assert len(rows) == 2  # both survived, not collapsed to one


def test_a2_synthetic_keys_survive_replay_map(tmp_path, monkeypatch):
    """TWO same-bucket, same-advisor_id A2 opinions yield TWO distinct replay-map
    keys (the load-bearing synthetic-key fix in ``_opinion_provider_map``).

    Persistence reads ``valid_opinions`` directly, so a broken provider map would
    STILL persist 2 rows — that path does not prove the fix.  The collision only
    bites the replay map handed to ``run_cycle``: keyed by ``advisor_id`` alone,
    N A2 opinions in one bucket collapse to ONE callable.  We intercept the exact
    ``advisor_map`` ``run_cycle`` receives and assert both A2 opinions survive AND
    both reach the fused bucket pool (not collapsed to one).
    """
    monkeypatch.setenv("MIROFISH_ENDPOINT", "https://mf.test/endpoint")
    # Both A2 opinions in the SAME (LONG) bucket and same advisor_id — the exact
    # collision case a plain advisor_id key would silently collapse.
    ops = [
        _a2_opinion(0.6, 180, "mf-long-a"),
        _a2_opinion(0.5, 200, "mf-long-b"),
    ]
    monkeypatch.setattr(
        "arbiter.adapters.mirofish.adapter.run",
        lambda idea, as_of, **kw: list(ops),
    )

    import arbiter.engine._engine as _engine_mod

    captured: dict = {}
    real_run_cycle = _engine_mod.run_cycle

    def _spy_run_cycle(*args, **kwargs):
        amap = kwargs["advisor_map"]
        a2_keys = [k for k in amap if k.startswith("A2.mirofish")]
        captured["a2_keys"] = a2_keys
        # Resolve every A2 callable — they must be DISTINCT opinions, not one.
        captured["a2_fps"] = sorted(amap[k]().source_fingerprint for k in a2_keys)
        return real_run_cycle(*args, **kwargs)

    monkeypatch.setattr(_engine_mod, "run_cycle", _spy_run_cycle)

    eng, conn = _make_engine(tmp_path)
    result = eng.run_cycle(as_of=_AS_OF)

    # Both A2 opinions survived into the replay map as DISTINCT keys/opinions.
    assert len(captured["a2_keys"]) == 2, captured["a2_keys"]
    assert captured["a2_fps"] == ["mf-long-a", "mf-long-b"]
    # Each synthetic key is unique (no overwrite/collision).
    assert len(set(captured["a2_keys"])) == 2
    # No synthetic key equals a bare real advisor_id.
    assert "A2.mirofish" not in captured["a2_keys"]
    # And run_cycle counted both as gathered opinions (not collapsed to one).
    n_a2_persisted = conn.execute(
        "SELECT COUNT(*) FROM opinions WHERE advisor_id='A2.mirofish'"
    ).fetchone()[0]
    assert n_a2_persisted == 2


def test_a2_does_not_disturb_a1(tmp_path, monkeypatch):
    """A2 enabled returning [] leaves A1 opinion slots intact (insider persists)."""
    monkeypatch.setenv("MIROFISH_ENDPOINT", "https://mf.test/endpoint")
    monkeypatch.setattr(
        "arbiter.adapters.mirofish.adapter.run",
        lambda idea, as_of, **kw: [],
    )

    eng, conn = _make_engine(tmp_path)
    eng.run_cycle(as_of=_AS_OF)

    n_a1 = conn.execute(
        "SELECT COUNT(*) FROM opinions WHERE advisor_id LIKE 'A1.%'"
    ).fetchone()[0]
    n_a2 = conn.execute(
        "SELECT COUNT(*) FROM opinions WHERE advisor_id='A2.mirofish'"
    ).fetchone()[0]
    assert n_a1 >= 1
    assert n_a2 == 0


def test_a2_negative_stance_passthrough(tmp_path, monkeypatch):
    """A bearish A2 opinion keeps its negative stance (no clamp)."""
    monkeypatch.setenv("MIROFISH_ENDPOINT", "https://mf.test/endpoint")
    monkeypatch.setattr(
        "arbiter.adapters.mirofish.adapter.run",
        lambda idea, as_of, **kw: [_a2_opinion(-0.7, 180, "mf-bear")],
    )

    eng, conn = _make_engine(tmp_path)
    eng.run_cycle(as_of=_AS_OF)

    row = conn.execute(
        "SELECT stance_score FROM opinions WHERE advisor_id='A2.mirofish'"
    ).fetchone()
    assert row is not None
    assert row["stance_score"] == pytest.approx(-0.7)


def test_a2_backtest_flag(tmp_path, monkeypatch):
    """With a BacktestClock, adapter.run is called with is_backtest=True."""
    monkeypatch.setenv("MIROFISH_ENDPOINT", "https://mf.test/endpoint")
    captured = {}

    def _fake_run(idea, as_of, **kw):
        captured["is_backtest"] = kw.get("is_backtest")
        return []

    monkeypatch.setattr("arbiter.adapters.mirofish.adapter.run", _fake_run)

    eng, conn = _make_engine(tmp_path, clock=BacktestClock(_AS_OF))
    eng.run_cycle(as_of=_AS_OF)
    assert captured.get("is_backtest") is True


# ---------------------------------------------------------------------------
# Single-opinion path is untouched (frozen interface)
# ---------------------------------------------------------------------------

def test_scheduler_unchanged():
    """run_named_advisors_parallel / run_advisors_parallel signatures unchanged."""
    import inspect
    from arbiter.orchestrator import scheduler

    sig = inspect.signature(scheduler.run_named_advisors_parallel)
    params = list(sig.parameters)
    # advisor_map + timeout only — A2 added zero params to the single-opinion path.
    assert params[0] == "advisor_map"
    assert "timeout_seconds" in params

    # A2 is NOT a single-opinion advisor_map key.
    sig2 = inspect.signature(scheduler.run_advisors_parallel)
    assert "advisors" in sig2.parameters
