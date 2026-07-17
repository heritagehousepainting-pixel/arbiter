"""Engine-level decision-funnel tracing tests (unfreeze Stage 1) — OFFLINE.

Verifies that a full ``run_cycle`` emits the WHY-no-trade audit trail:
  - ``decide.skip`` audit events with a reason code when an idea dies
    (here: sizing fails closed on missing ADV → ``size_zero``), and
  - one ``cycle_funnel`` audit event summarising the cycle
    (ideas / no_opinions / flat_conviction / size_zero / submitted), plus
  - ``engine.last_cycle_funnel`` mirroring the same counts (consumed by the
    daemon's idle-capital alert).

All offline: temp SQLite + BacktestClock + FixtureSource PIT.  A5 gather is
monkeypatched to inject a deterministic opinion (same pattern as
``test_a5_wiring.py``).
"""
from __future__ import annotations

import dataclasses
from datetime import datetime, timedelta, timezone
from pathlib import Path

from arbiter.config import load_config
from arbiter.contract.opinion import Opinion
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.audit import read_audit
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.engine import build_engine
from arbiter.types import ConfidenceSource

_UTC = timezone.utc
_AS_OF = datetime(2025, 3, 15, 12, 0, 0, tzinfo=_UTC)


def _build_pit_no_adv(ticker: str) -> PITGateway:
    """PIT with prices but NO adv_20d → sizing fails closed (size_zero)."""
    fx = FixtureSource()
    ts = _AS_OF - timedelta(days=1)
    fx.add("price_close", ticker, ts, 300.0)
    fx.add("price_open", ticker, ts, 300.0)
    fx.add("spread", ticker, ts, 0.01)
    pit = PITGateway()
    for src in ("price_close", "price_open", "spread", "adv_20d"):
        pit.register_source(src, fx)
    return pit


def _make_engine(tmp_path: Path, ticker: str):
    db_path = str(tmp_path / "funnel.db")
    config = dataclasses.replace(
        load_config(), live_trading=False, executor_backend="sim",
        db_path=db_path, audit_path=str(tmp_path / "audit.jsonl"),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        kill_switch_url="", alert_webhook_url="",
        robotics_advisor_enabled=True,
    )
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    pit = _build_pit_no_adv(ticker)
    eng = build_engine(config, conn=conn, pit=pit, clock=BacktestClock(_AS_OF))
    return eng, conn, config


def _a5_opinion(ticker: str, stance: float) -> Opinion:
    return Opinion(
        advisor_id="A5.robotics", ticker=ticker, stance_score=stance, confidence=0.25,
        confidence_source=ConfidenceSource.MODELED, horizon_days=7, as_of=_AS_OF,
        rationale="robotics", source_fingerprint="a5-fp", run_group_id="a5-run",
    )


def test_size_zero_idea_emits_skip_and_funnel(tmp_path, monkeypatch):
    """A strong opinion whose sizing fails closed (no ADV) leaves a full trail:
    decide.skip(reason=size_zero) + cycle_funnel + engine.last_cycle_funnel."""
    eng, conn, config = _make_engine(tmp_path, "NVDA")
    monkeypatch.setattr(
        "arbiter.adapters.a5.gather_a5_opinions",
        lambda conn, clock, config: [_a5_opinion("NVDA", 0.8)],
    )
    result = eng.run_cycle(as_of=_AS_OF)
    assert result.orders_submitted == 0

    events = read_audit(config.audit_path)
    skips = [e for e in events if e["event"] == "decide.skip"]
    assert any(
        e["payload"].get("reason") == "size_zero" and e["payload"].get("ticker") == "NVDA"
        for e in skips
    ), f"no size_zero decide.skip in {skips}"

    funnels = [e for e in events if e["event"] == "cycle_funnel"]
    assert len(funnels) == 1, f"expected exactly one cycle_funnel, got {len(funnels)}"
    funnel = funnels[0]["payload"]
    assert funnel["ideas"] >= 1
    assert funnel["size_zero"] >= 1
    assert funnel["submitted"] == 0

    assert eng.last_cycle_funnel == funnel


def test_funnel_emitted_even_on_empty_cycle(tmp_path):
    """A cycle with no signals/opinions still emits cycle_funnel (all zeros
    except dedupe/no-op counts) so the funnel is greppable every cycle."""
    eng, conn, config = _make_engine(tmp_path, "NVDA")
    eng.run_cycle(as_of=_AS_OF)
    events = read_audit(config.audit_path)
    funnels = [e for e in events if e["event"] == "cycle_funnel"]
    assert len(funnels) == 1
    assert funnels[0]["payload"]["submitted"] == 0
