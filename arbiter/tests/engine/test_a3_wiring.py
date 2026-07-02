"""Engine-level wiring tests for the A3 (news) advisor — OFFLINE.

Verifies the risk-critical engine integration (the part NOT covered by the
adapters/a3 pipeline unit tests):
  - A3 is a clean no-op when no Finnhub key is set (real gather self-gates → []).
  - When A3 produces a corroborated opinion, run_cycle SPAWNS its own
    SHORT-bucket idea and PERSISTS the A3 opinion LINKED to that idea (so it can
    earn trust) — i.e. it does NOT orphan (the attribution bug caught in audit).

All offline: temp SQLite + BacktestClock + FixtureSource PIT; ``gather_a3_opinions``
is monkeypatched, so no network and the backtest network-gate is bypassed for the
wiring assertion.
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


def _make_engine(tmp_path: Path):
    db_path = str(tmp_path / "a3.db")
    config = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="sim",
        db_path=db_path, audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="", alert_webhook_url="",
        # This test proves news-only DISCOVERY spawns + links an idea — that
        # path only exists with the Tier-3 #12 catalyst gate disabled.
        a3_catalyst_only=False,
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    pit = _build_pit("MSFT")
    eng = build_engine(config, conn=conn, pit=pit, clock=BacktestClock(_AS_OF))
    return eng, conn


def _a3_opinion(ticker="MSFT", stance=0.5) -> Opinion:
    return Opinion(
        advisor_id="A3.news", ticker=ticker, stance_score=stance, confidence=0.6,
        confidence_source=ConfidenceSource.MODELED, horizon_days=7, as_of=_AS_OF,
        rationale="news", source_fingerprint="a3-fp", run_group_id="a3-run",
    )


def test_a3_inert_when_unset(tmp_path, monkeypatch):
    """No Finnhub key → the real gather self-gates to [] → no A3 opinions, no
    A3 ideas, cycle behaves as if A3 did not exist."""
    import httpx
    monkeypatch.setattr(
        httpx, "get",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("network called")),
    )
    eng, conn = _make_engine(tmp_path)
    eng.run_cycle(as_of=_AS_OF)
    n_a3 = conn.execute(
        "SELECT COUNT(*) FROM opinions WHERE advisor_id = 'A3.news'"
    ).fetchone()[0]
    assert n_a3 == 0


def test_a3_spawns_short_idea_and_links_opinion(tmp_path, monkeypatch):
    """A corroborated A3 opinion (no A1 signal at all) drives its own SHORT idea
    and the opinion persists LINKED to it (idea_id set) — not orphaned."""
    eng, conn = _make_engine(tmp_path)
    # Inject one corroborated A3 opinion for a watchlist ticker (MSFT), bypassing
    # the network + backtest gate. (MSFT is in _DEFAULT_WATCHLIST.)
    monkeypatch.setattr(
        "arbiter.adapters.a3.gather_a3_opinions",
        lambda conn, clock, config, watchlist: [_a3_opinion("MSFT", 0.5)],
    )
    eng.run_cycle(as_of=_AS_OF)

    # A SHORT-bucket idea was spawned for MSFT.
    idea = conn.execute(
        "SELECT idea_id, dedupe_key_bucket FROM ideas "
        "WHERE dedupe_key_ticker = 'MSFT' AND is_superseded = 0"
    ).fetchone()
    assert idea is not None, "A3 did not spawn an idea for MSFT"
    assert idea["dedupe_key_bucket"] == "SHORT"

    # The A3 opinion was persisted AND linked to that idea (not orphaned).
    op = conn.execute(
        "SELECT advisor_id, idea_id FROM opinions WHERE advisor_id = 'A3.news' "
        "AND ticker = 'MSFT'"
    ).fetchone()
    assert op is not None, "A3 opinion was not persisted"
    assert op["idea_id"] == idea["idea_id"], "A3 opinion orphaned from its idea"
