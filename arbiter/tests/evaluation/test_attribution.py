"""Tests for arbiter.evaluation.attribution — sub-project #5a (real attribution).

Per-advisor outcome fan-out, per-(idea,advisor) idempotency, and the
no-opinion proxy fallback (with the attribution.fallback_proxy metric).

OFFLINE: FAKE in-memory PIT (FixtureSource), injected as_of/now, no network.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import Idea
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.evaluation import attribution
from arbiter.evaluation.outcome_labeler import _next_trading_day, _on_or_next_trading_day
from arbiter.evaluation.outcome_store import query_outcomes
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.signals import opinion_store
from arbiter.types import ConfidenceSource, IdeaState

UTC = timezone.utc


def _ts(y, m, d) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    c = get_connection(str(tmp_path / "attr.db"))
    run_migrations(c)
    return c


def _idea(idea_id="IDEA-1", ticker="AAPL", horizon_days=30, *, as_of, bucket="SHORT") -> Idea:
    return Idea(
        idea_id=idea_id,
        ticker=ticker,
        thesis="t",
        horizon_days=horizon_days,
        state=IdeaState.MONITORED,
        as_of=as_of,
        dedupe_key=(ticker, bucket),
    )


def _build_pit(ticker, *, t0, horizon_days, entry_open=100.0, exit_close=110.0,
               spy_entry=400.0, spy_exit=402.0) -> PITGateway:
    open_src = FixtureSource()
    close_src = FixtureSource()
    t1 = _next_trading_day(t0)
    open_src.add("price_open", ticker, t1, entry_open)
    open_src.add("price_open", "SPY", t1, spy_entry)
    exit_as_of = _on_or_next_trading_day(t0 + timedelta(days=horizon_days))
    close_src.add("price_close", ticker, exit_as_of, exit_close)
    close_src.add("price_close", "SPY", exit_as_of, spy_exit)
    for i in range(400, 0, -1):
        day = t0 - timedelta(days=i)
        close_src.add("price_close", ticker, day, entry_open)
        close_src.add("price_close", "SPY", day, spy_entry)
    pit = PITGateway()
    pit.register_source("price_open", open_src)
    pit.register_source("price_close", close_src)
    return pit


def _seed_opinion(conn, *, advisor_id, ticker, stance, confidence, idea_id, as_of, fp):
    op = Opinion(
        advisor_id=advisor_id, ticker=ticker, stance_score=stance,
        confidence=confidence, confidence_source=ConfidenceSource.MODELED,
        horizon_days=30, as_of=as_of, rationale="t",
        source_fingerprint=fp, run_group_id="rg",
    )
    opinion_store.persist_opinion(conn, op, idea_id=idea_id, as_of=as_of)


class _Metrics:
    def __init__(self):
        self.events = []

    def record(self, event, payload, *, recorded_at=""):
        self.events.append((event, payload))


class TestFanOut:
    def test_two_opinions_two_outcomes_each_with_own_stance(self, conn):
        t0 = _ts(2025, 1, 10)
        idea = _idea(as_of=t0)
        _seed_opinion(conn, advisor_id="A1.insider", ticker="AAPL", stance=0.9,
                      confidence=0.8, idea_id=idea.idea_id, as_of=t0, fp="fp-i")
        _seed_opinion(conn, advisor_id="A1.congress", ticker="AAPL", stance=0.3,
                      confidence=0.5, idea_id=idea.idea_id, as_of=t0, fp="fp-c")
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)

        ids = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, label_kind="normal",
        )
        assert len(ids) == 2

        rows = query_outcomes(conn, idea_id=idea.idea_id)
        by_advisor = {r["advisor_id"]: r for r in rows}
        assert set(by_advisor) == {"A1.insider", "A1.congress"}
        # Each carries ITS OWN stance + confidence...
        assert by_advisor["A1.insider"]["stance_score"] == pytest.approx(0.9)
        assert by_advisor["A1.insider"]["advisor_confidence"] == pytest.approx(0.8)
        assert by_advisor["A1.congress"]["stance_score"] == pytest.approx(0.3)
        assert by_advisor["A1.congress"]["advisor_confidence"] == pytest.approx(0.5)
        # ...but the SAME realized alpha (same entry/exit/beta).
        assert by_advisor["A1.insider"]["alpha_bps"] == pytest.approx(
            by_advisor["A1.congress"]["alpha_bps"]
        )

    def test_single_opinion_uses_real_stance_not_proxy(self, conn):
        t0 = _ts(2025, 2, 3)
        idea = _idea(as_of=t0)
        _seed_opinion(conn, advisor_id="A1.congress", ticker="AAPL", stance=0.42,
                      confidence=0.66, idea_id=idea.idea_id, as_of=t0, fp="fp-c")
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)

        # A proxy fallback is supplied but must NOT be used (opinion exists).
        ids = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, label_kind="normal",
            fallback_advisor_id_for=lambda i: "A1.insider",
        )
        assert len(ids) == 1
        rows = query_outcomes(conn, idea_id=idea.idea_id)
        assert rows[0]["advisor_id"] == "A1.congress"  # real opinion, not proxy
        assert rows[0]["stance_score"] == pytest.approx(0.42)


class TestIdempotency:
    def test_partial_fanout_then_retry_writes_missing_only(self, conn):
        t0 = _ts(2025, 3, 1)
        idea = _idea(as_of=t0)
        _seed_opinion(conn, advisor_id="A1.insider", ticker="AAPL", stance=0.9,
                      confidence=0.8, idea_id=idea.idea_id, as_of=t0, fp="fp-i")
        _seed_opinion(conn, advisor_id="A1.congress", ticker="AAPL", stance=0.3,
                      confidence=0.5, idea_id=idea.idea_id, as_of=t0, fp="fp-c")
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)

        # Simulate a partial write: store ONLY the insider outcome directly.
        from arbiter.evaluation import outcome_labeler, outcome_store
        partial = outcome_labeler.label(
            idea, pit=pit, cutoff_as_of=now, advisor_id="A1.insider",
            advisor_confidence=0.8, stance_score=0.9, label_kind="normal",
        )
        outcome_store.store_outcome(partial, conn, as_of=now)

        # Resolver re-runs: must write ONLY the missing congress advisor.
        ids = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, label_kind="normal",
        )
        assert len(ids) == 1
        rows = query_outcomes(conn, idea_id=idea.idea_id)
        assert len(rows) == 2  # no duplicate insider
        assert {r["advisor_id"] for r in rows} == {"A1.insider", "A1.congress"}

    def test_double_resolve_no_duplicates(self, conn):
        t0 = _ts(2025, 4, 1)
        idea = _idea(as_of=t0)
        _seed_opinion(conn, advisor_id="A1.insider", ticker="AAPL", stance=0.9,
                      confidence=0.8, idea_id=idea.idea_id, as_of=t0, fp="fp-i")
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)
        attribution.resolve_advisor_outcomes(conn, idea, pit=pit, cutoff_as_of=now)
        second = attribution.resolve_advisor_outcomes(conn, idea, pit=pit, cutoff_as_of=now)
        assert second == []  # nothing new
        assert len(query_outcomes(conn, idea_id=idea.idea_id)) == 1


class TestFallback:
    def test_no_opinion_uses_proxy_and_increments_metric(self, conn):
        t0 = _ts(2025, 5, 1)
        idea = _idea(idea_id="LEGACY", as_of=t0)  # no persisted opinion
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)
        metrics = _Metrics()

        ids = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, label_kind="normal",
            metrics=metrics, fallback_advisor_id_for=lambda i: "A1.insider",
        )
        assert len(ids) == 1
        rows = query_outcomes(conn, idea_id="LEGACY")
        # E3: the neutral fallback uses the RESERVED PROXY.* namespace so it can
        # never collide with / mask the real advisor's per-(idea,advisor) outcome.
        assert rows[0]["advisor_id"] == "PROXY.A1.insider"
        assert rows[0]["stance_score"] == pytest.approx(0.0)  # neutral
        # Metric fired.
        assert any(e == "attribution.fallback_proxy" for e, _ in metrics.events)

    def test_proxy_does_not_mask_real_advisor_on_recovery(self, conn):
        """E3: after the neutral proxy fires, recovering the REAL opinion must
        still write a real per-advisor outcome (the proxy id can't collide)."""
        t0 = _ts(2025, 5, 1)
        idea = _idea(idea_id="REC", as_of=t0)
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)

        # First pass: no opinion → neutral PROXY.* fallback for A1.insider.
        ids1 = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, label_kind="normal",
            fallback_advisor_id_for=lambda i: "A1.insider",
        )
        assert len(ids1) == 1
        rows1 = query_outcomes(conn, idea_id="REC")
        assert {r["advisor_id"] for r in rows1} == {"PROXY.A1.insider"}

        # Now the real opinion is recovered (persisted) for A1.insider.
        _seed_opinion(
            conn, advisor_id="A1.insider", ticker="AAPL", stance=0.85,
            confidence=0.8, idea_id="REC", as_of=t0, fp="fp-real",
        )

        # Second pass: the real A1.insider outcome is written (NOT masked by the
        # proxy), because PROXY.A1.insider != A1.insider in the existence guard.
        ids2 = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, label_kind="normal",
            fallback_advisor_id_for=lambda i: "A1.insider",
        )
        assert len(ids2) == 1
        rows2 = query_outcomes(conn, idea_id="REC")
        advisors = {r["advisor_id"] for r in rows2}
        assert "A1.insider" in advisors          # real stance recovered
        assert "PROXY.A1.insider" in advisors    # proxy retained, didn't collide
        real_row = next(r for r in rows2 if r["advisor_id"] == "A1.insider")
        assert real_row["stance_score"] == pytest.approx(0.85)  # the TRUE stance

    def test_no_opinion_no_fallback_writes_nothing(self, conn):
        t0 = _ts(2025, 5, 1)
        idea = _idea(idea_id="ORPHAN", as_of=t0)
        pit = _build_pit("AAPL", t0=t0, horizon_days=30)
        now = t0 + timedelta(days=35)
        ids = attribution.resolve_advisor_outcomes(
            conn, idea, pit=pit, cutoff_as_of=now, fallback_advisor_id_for=None,
        )
        assert ids == []
        assert query_outcomes(conn, idea_id="ORPHAN") == []
