"""Engine-level wiring tests for the A5.robotics advisor — OFFLINE.

Verifies the engine integration (not covered by the adapters/a5 unit tests):
  - A5 is inert by default: with the kill-switch OFF (the shipped default) a
    persisted trigger-hit produces NO A5 opinions — the seam is dormant.
  - When A5 produces an opinion, run_cycle SPAWNS its own SHORT-bucket idea and
    PERSISTS the A5 opinion LINKED to that idea (so it can earn trust) — it does
    NOT orphan.

All offline: temp SQLite + BacktestClock + FixtureSource PIT. For the wiring
assertion ``gather_a5_opinions`` is monkeypatched, bypassing the network and the
backtest look-ahead gate.
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbiter.config import load_config
from arbiter.contract.opinion import Opinion
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.robotics_signal.store import persist_signals
from arbiter.robotics_signal.types import RoboticsDevelopment
from arbiter.types import ConfidenceSource

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


def _build_pit(ticker: str):
    fx = FixtureSource()
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, 300.0)
    fx.add("price_open", ticker, ts, 300.0)
    fx.add("spread", ticker, ts, 0.01)
    fx.add("adv_20d", ticker, ts, 10_000_000.0)
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fx)
    return pit


def _make_engine(tmp_path: Path, *, robotics_advisor_enabled: bool, ticker: str):
    db_path = str(tmp_path / "a5.db")
    config = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="sim",
        db_path=db_path, audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="", alert_webhook_url="",
        robotics_advisor_enabled=robotics_advisor_enabled,
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    pit = _build_pit(ticker)
    eng = build_engine(config, conn=conn, pit=pit, clock=BacktestClock(_AS_OF))
    return eng, conn


def _a5_opinion(ticker="NVDA", stance=0.5) -> Opinion:
    return Opinion(
        advisor_id="A5.robotics", ticker=ticker, stance_score=stance, confidence=0.25,
        confidence_source=ConfidenceSource.MODELED, horizon_days=7, as_of=_AS_OF,
        rationale="robotics", source_fingerprint="a5-fp", run_group_id="a5-run",
    )


def _persist_hit(conn, ticker="NVDA"):
    persist_signals(conn, [RoboticsDevelopment(
        headline="trigger fired", summary="s", category="integrator",
        symbols=[ticker], trigger_hit=True, trigger_name=ticker,
        sources=["https://ex/x"])], _AS_OF)


def test_a5_inert_when_disabled(tmp_path):
    """Kill-switch OFF (default) → a persisted trigger-hit produces no A5 opinion,
    and the cycle runs without error."""
    eng, conn = _make_engine(tmp_path, robotics_advisor_enabled=False, ticker="NVDA")
    _persist_hit(conn, "NVDA")
    eng.run_cycle(as_of=_AS_OF)
    n_a5 = conn.execute(
        "SELECT COUNT(*) FROM opinions WHERE advisor_id = 'A5.robotics'"
    ).fetchone()[0]
    assert n_a5 == 0


def test_a5_spawns_short_idea_and_links_opinion(tmp_path, monkeypatch):
    """An A5.robotics opinion (no A1 signal) drives its own SHORT idea and the
    opinion persists LINKED to it (idea_id set) — not orphaned."""
    eng, conn = _make_engine(tmp_path, robotics_advisor_enabled=True, ticker="NVDA")
    monkeypatch.setattr(
        "arbiter.adapters.a5.gather_a5_opinions",
        lambda conn, clock, config: [_a5_opinion("NVDA", 0.5)],
    )
    eng.run_cycle(as_of=_AS_OF)

    idea = conn.execute(
        "SELECT idea_id, dedupe_key_bucket FROM ideas "
        "WHERE dedupe_key_ticker = 'NVDA' AND is_superseded = 0"
    ).fetchone()
    assert idea is not None, "A5 did not spawn an idea for NVDA"
    assert idea["dedupe_key_bucket"] == "SHORT"

    op = conn.execute(
        "SELECT advisor_id, idea_id FROM opinions WHERE advisor_id = 'A5.robotics' "
        "AND ticker = 'NVDA'"
    ).fetchone()
    assert op is not None, "A5 opinion was not persisted"
    assert op["idea_id"] == idea["idea_id"], "A5 opinion orphaned from its idea"
