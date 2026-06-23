"""Tests for arbiter.orchestrator.outcome_runner — WP-C (Phase-2 persistence).

Drives the persistence-backed outcome sweep:

  1. load MONITORED ideas from the DB
  2. sweep_outcomes advances eligible ones MONITORED -> OUTCOME_READY (in memory)
  3. attempt to label via PIT FIRST, then persist on success only
       - price available  -> persist OUTCOME_READY, store outcome, CLOSE the
                             idea, return its id
       - LookupError      -> log + continue, persist NOTHING; the durable row
                             stays MONITORED so the idea is retried next sweep
  4. return the list of stored outcome ids

All tests are OFFLINE: a FAKE in-memory PIT (FixtureSource) and a BacktestClock.
NO network, NO datetime.now().

Covers:
  - empty MONITORED set -> []
  - one idea past horizon with prices -> outcome stored + idea CLOSED + id returned
  - one idea past horizon, prices MISSING -> no crash, no outcome, idea stays
    MONITORED in the durable store; a re-run with prices available labels it
  - one idea NOT yet at horizon -> untouched (still MONITORED, no outcome)
  - mixed batch (labelable + LookupError + not-ready) processed correctly
  - CLOSED / OUTCOME_READY persistence verified by reloading via idea_store
  - advisor_id_for + advisor_confidence_for callables threaded through to the outcome
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.evaluation.outcome_labeler import _next_trading_day, _on_or_next_trading_day
from arbiter.evaluation.outcome_store import query_outcomes
from arbiter.orchestrator.idea_store import (
    load_ideas_by_state,
    persist_new_idea,
)
from arbiter.orchestrator.outcome_runner import run_outcome_sweep
from arbiter.types import IdeaState

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path: Path) -> sqlite3.Connection:
    """Migrated SQLite connection (fresh per test)."""
    db_path = str(tmp_path / "test_outcome_runner.db")
    c = get_connection(db_path)
    run_migrations(c)
    return c


@pytest.fixture()
def audit_file(tmp_path: Path) -> Path:
    return tmp_path / "test_audit.jsonl"


def _ts(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


def _make_idea(
    *,
    idea_id: str,
    ticker: str = "AAPL",
    horizon_days: int = 30,
    as_of: datetime,
    state: IdeaState = IdeaState.MONITORED,
    dedupe_key: tuple[str, str] | None = None,
) -> Idea:
    return Idea(
        idea_id=idea_id,
        ticker=ticker,
        thesis="test thesis",
        horizon_days=horizon_days,
        state=state,
        as_of=as_of,
        dedupe_key=dedupe_key or (ticker, "SHORT"),
    )


def _build_pit_for(
    ticker: str,
    *,
    t0: datetime,
    horizon_days: int,
    entry_open: float = 100.0,
    exit_close: float = 105.0,
    spy_entry_open: float = 400.0,
    spy_exit_close: float = 402.0,
) -> tuple[FixtureSource, FixtureSource]:
    """Return (open_src, close_src) FixtureSources priced so that ``label`` succeeds.

    Mirrors the labeler's trading-day arithmetic so the fixture timestamps land
    on the exact ask-dates (next trading day open, on-or-next trading day close)
    and includes a 400-day beta window of uniform closes (beta imputes to 1.0).
    """
    open_src = FixtureSource()
    close_src = FixtureSource()

    t1_entry = _next_trading_day(t0)
    open_src.add("price_open", ticker, t1_entry, entry_open)
    open_src.add("price_open", "SPY", t1_entry, spy_entry_open)

    raw_exit = t0 + timedelta(days=horizon_days)
    exit_as_of = _on_or_next_trading_day(raw_exit)
    close_src.add("price_close", ticker, exit_as_of, exit_close)
    close_src.add("price_close", "SPY", exit_as_of, spy_exit_close)

    # Beta window (uniform -> imputed 1.0, no network).
    for i in range(400, 0, -1):
        day = t0 - timedelta(days=i)
        close_src.add("price_close", ticker, day, entry_open)
        close_src.add("price_close", "SPY", day, spy_entry_open)

    return open_src, close_src


def _advisor_id_for(idea: Idea) -> str:
    """Test stub mirroring the engine's horizon-based heuristic."""
    return "A1.congress" if idea.horizon_days >= 60 else "A1.insider"


# ---------------------------------------------------------------------------
# 1. Empty MONITORED set
# ---------------------------------------------------------------------------

class TestEmpty:
    def test_no_monitored_ideas_returns_empty(self, conn):
        clock = BacktestClock(_ts(2025, 6, 1))
        pit = PITGateway()
        result = run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )
        assert result == []

    def test_only_non_monitored_ideas_returns_empty(self, conn):
        # A CLOSED idea past its horizon must NOT be swept.
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_CLOSED", as_of=_ts(2025, 1, 1),
                       state=IdeaState.CLOSED),
            created_at=_ts(2025, 1, 1),
        )
        clock = BacktestClock(_ts(2025, 6, 1))
        result = run_outcome_sweep(
            conn, pit=PITGateway(), clock=clock, advisor_id_for=_advisor_id_for
        )
        assert result == []


# ---------------------------------------------------------------------------
# 2. Idea past horizon, prices available -> stored + CLOSED + id returned
# ---------------------------------------------------------------------------

class TestLabelable:
    """FALLBACK-PATH tests (D4 proxy) — these do NOT seed a persisted opinion.

    No opinion is written to the store, so the sweep cannot attribute via the
    real persisted-stance path; it falls back to the ``advisor_id_for`` proxy
    heuristic (``_advisor_id_for``: horizon>=60 -> A1.congress else A1.insider)
    and a default/proxy stance.  Assertions like ``count == 1`` and
    ``advisor_id == "A1.insider"`` therefore exercise the outcome-sweep FSM
    mechanics and the fallback attribution path — they are NOT coverage of real
    persisted-opinion attribution.  Genuine attribution (persisted opinion ->
    advisor + stance) is covered by ``TestRealAttribution`` below.
    """

    def test_past_horizon_with_prices_closes_idea(self, conn):
        # FALLBACK PATH: no persisted opinion seeded -> advisor + stance come
        # from the proxy, not real attribution. Asserts FSM/close mechanics.
        t0 = _ts(2025, 1, 10)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_GOOD", ticker="AAPL", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )

        open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)

        # clock well past horizon expiry
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        result = run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )

        # One outcome id returned.
        assert len(result) == 1
        oid = result[0]
        assert isinstance(oid, str)

        # Idea persisted as CLOSED (verify by reload).
        assert load_ideas_by_state(conn, {IdeaState.MONITORED}) == []
        assert load_ideas_by_state(conn, {IdeaState.OUTCOME_READY}) == []
        closed = load_ideas_by_state(conn, {IdeaState.CLOSED})
        assert len(closed) == 1
        assert closed[0].idea_id == "I_GOOD"

        # Outcome row persisted with the right idea + advisor.
        outcomes = query_outcomes(conn, idea_id="I_GOOD")
        assert len(outcomes) == 1
        # E3: no persisted opinion → neutral fallback uses the RESERVED PROXY.*
        # namespace (so it can't mask the real per-advisor outcome on recovery).
        assert outcomes[0]["advisor_id"] == "PROXY.A1.insider"  # horizon < 60
        assert outcomes[0]["label_kind"] == "normal"

    def test_advisor_confidence_for_threaded_through(self, conn):
        t0 = _ts(2025, 2, 3)
        horizon = 90  # >= 60 -> A1.congress
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_CONF", ticker="MSFT", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        open_src, close_src = _build_pit_for("MSFT", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        def conf_for(idea: Idea) -> float:
            return 0.73

        result = run_outcome_sweep(
            conn, pit=pit, clock=clock,
            advisor_id_for=_advisor_id_for,
            advisor_confidence_for=conf_for,
        )
        assert len(result) == 1
        outcomes = query_outcomes(conn, idea_id="I_CONF")
        # E3: reserved PROXY.* namespace for the no-opinion neutral fallback.
        assert outcomes[0]["advisor_id"] == "PROXY.A1.congress"
        assert outcomes[0]["advisor_confidence"] == pytest.approx(0.73)

    def test_default_confidence_is_one(self, conn):
        t0 = _ts(2025, 3, 4)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_DEFCONF", ticker="NVDA", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        open_src, close_src = _build_pit_for("NVDA", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )
        outcomes = query_outcomes(conn, idea_id="I_DEFCONF")
        assert outcomes[0]["advisor_confidence"] == pytest.approx(1.0)

    def test_outcome_audit_path_honored(self, conn, audit_file):
        from arbiter.db.audit import read_audit

        t0 = _ts(2025, 4, 1)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_AUD", ticker="AAPL", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        run_outcome_sweep(
            conn, pit=pit, clock=clock,
            advisor_id_for=_advisor_id_for, audit_path=audit_file,
        )
        records = read_audit(audit_file)
        events = {r["event"] for r in records}
        # Both the OUTCOME_READY/CLOSED transitions and the outcome insert
        # should have flowed to the same audit file.
        assert "idea_state_transition" in events
        assert "insert_outcome" in events


# ---------------------------------------------------------------------------
# 3. Idea past horizon, prices MISSING -> LookupError handled gracefully
# ---------------------------------------------------------------------------

class TestMissingPrice:
    def test_lookuperror_leaves_idea_monitored(self, conn, caplog):
        """P1 orphan-bug guard: a price LookupError must persist NOTHING.

        The idea is the live case — still inside its price window when first
        swept.  Labeling raises LookupError; the durable row MUST stay
        MONITORED (NOT OUTCOME_READY) so the sweep, which only ever reloads
        MONITORED ideas, reconsiders it on the next run.  Persisting
        OUTCOME_READY up-front would orphan it forever.
        """
        t0 = _ts(2025, 5, 1)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_NOPRICE", ticker="GHOST", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        # Empty PIT -> label() raises LookupError on the entry open.
        pit = PITGateway()
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        with caplog.at_level(logging.WARNING):
            result = run_outcome_sweep(
                conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
            )

        # No crash, no outcome stored, nothing returned.
        assert result == []
        assert query_outcomes(conn, idea_id="I_NOPRICE") == []

        # The idea is STILL MONITORED in the durable store — NOT orphaned in
        # OUTCOME_READY and NOT CLOSED.
        monitored = load_ideas_by_state(conn, {IdeaState.MONITORED})
        assert len(monitored) == 1
        assert monitored[0].idea_id == "I_NOPRICE"
        assert load_ideas_by_state(conn, {IdeaState.OUTCOME_READY}) == []
        assert load_ideas_by_state(conn, {IdeaState.CLOSED}) == []

        # A warning was logged.
        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_rerun_labels_once_prices_available(self, conn):
        """Because the LookupError run left the idea MONITORED, a later sweep
        (now with prices) picks it up and labels it: CLOSED + outcome stored.

        This is the proof the orphan bug is gone — the idea is NOT lost after
        the first failed attempt.
        """
        t0 = _ts(2025, 5, 1)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_RETRY", ticker="AAPL", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )

        # --- First sweep: empty PIT -> LookupError -> stays MONITORED. ---
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))
        first = run_outcome_sweep(
            conn, pit=PITGateway(), clock=clock, advisor_id_for=_advisor_id_for
        )
        assert first == []
        assert {i.idea_id for i in load_ideas_by_state(conn, {IdeaState.MONITORED})} == {
            "I_RETRY"
        }

        # --- Second sweep: prices now available -> idea is labeled + CLOSED. ---
        open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)

        second = run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )
        assert len(second) == 1

        # Now CLOSED with exactly one outcome row — nothing left MONITORED.
        assert load_ideas_by_state(conn, {IdeaState.MONITORED}) == []
        assert load_ideas_by_state(conn, {IdeaState.OUTCOME_READY}) == []
        closed = load_ideas_by_state(conn, {IdeaState.CLOSED})
        assert {i.idea_id for i in closed} == {"I_RETRY"}
        assert len(query_outcomes(conn, idea_id="I_RETRY")) == 1


# ---------------------------------------------------------------------------
# 4. Idea not yet at horizon -> untouched
# ---------------------------------------------------------------------------

class TestNotReady:
    def test_idea_inside_horizon_untouched(self, conn):
        t0 = _ts(2025, 6, 1)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_FRESH", ticker="AAPL", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        # Prices exist, but the clock is well inside the horizon window.
        open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)
        clock = BacktestClock(t0 + timedelta(days=10))  # < horizon

        result = run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )

        assert result == []
        assert query_outcomes(conn, idea_id="I_FRESH") == []
        # Still MONITORED, untouched.
        monitored = load_ideas_by_state(conn, {IdeaState.MONITORED})
        assert len(monitored) == 1
        assert monitored[0].idea_id == "I_FRESH"
        assert load_ideas_by_state(conn, {IdeaState.OUTCOME_READY}) == []


# ---------------------------------------------------------------------------
# 5. Mixed batch
# ---------------------------------------------------------------------------

class TestMixedBatch:
    def test_mixed_batch_processed_correctly(self, conn, caplog):
        t0 = _ts(2025, 1, 6)
        horizon = 30

        # (a) labelable: past horizon, prices present
        persist_new_idea(
            conn,
            _make_idea(idea_id="M_GOOD", ticker="GOODCO", horizon_days=horizon, as_of=t0,
                       dedupe_key=("GOODCO", "SHORT")),
            created_at=t0,
        )
        # (b) LookupError: past horizon, NO prices
        persist_new_idea(
            conn,
            _make_idea(idea_id="M_MISS", ticker="MISSCO", horizon_days=horizon, as_of=t0,
                       dedupe_key=("MISSCO", "SHORT")),
            created_at=t0,
        )
        # (c) not ready: as_of recent so horizon not elapsed at clock time
        recent = t0 + timedelta(days=20)
        persist_new_idea(
            conn,
            _make_idea(idea_id="M_FRESH", ticker="FRESHCO", horizon_days=horizon, as_of=recent,
                       dedupe_key=("FRESHCO", "SHORT")),
            created_at=recent,
        )

        # Only GOODCO has prices in the PIT.
        open_src, close_src = _build_pit_for("GOODCO", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)

        # clock: past GOOD/MISS horizon (t0+30) but before FRESH horizon (recent+30).
        clock = BacktestClock(t0 + timedelta(days=horizon + 2))

        with caplog.at_level(logging.WARNING):
            result = run_outcome_sweep(
                conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
            )

        # Exactly one outcome stored (GOODCO).
        assert len(result) == 1
        assert len(query_outcomes(conn, idea_id="M_GOOD")) == 1
        assert query_outcomes(conn, idea_id="M_MISS") == []
        assert query_outcomes(conn, idea_id="M_FRESH") == []

        # GOODCO -> CLOSED
        assert {i.idea_id for i in load_ideas_by_state(conn, {IdeaState.CLOSED})} == {"M_GOOD"}
        # Nothing is orphaned in OUTCOME_READY.
        assert load_ideas_by_state(conn, {IdeaState.OUTCOME_READY}) == []
        # MISSCO (labeling failed -> nothing persisted) and FRESHCO (never
        # elapsed) are BOTH still MONITORED and will be retried next sweep.
        assert {i.idea_id for i in load_ideas_by_state(conn, {IdeaState.MONITORED})} == {
            "M_MISS",
            "M_FRESH",
        }


# ---------------------------------------------------------------------------
# B2 — sweep skips a MONITORED idea that has a SELL order row in flight
# ---------------------------------------------------------------------------

def test_sweep_skips_monitored_idea_with_sell_order(conn):
    """A MONITORED idea past horizon but with a (pending) SELL row must NOT be
    labeled by the horizon sweep — the exit monitor owns that close-out (B2)."""
    from arbiter.db.helpers import generate_ulid, insert_row
    from arbiter.orchestrator.idea_store import update_idea_state

    t0 = _ts(2025, 1, 1)
    horizon = 30
    idea = _make_idea(idea_id="M_SELLINFLIGHT", as_of=t0, horizon_days=horizon,
                      state=IdeaState.NASCENT, dedupe_key=("AAPL", "SHORT"))
    persist_new_idea(conn, idea, created_at=t0)
    conn.execute("UPDATE ideas SET state=? WHERE idea_id=?",
                 (IdeaState.MONITORED.value, "M_SELLINFLIGHT"))
    conn.commit()

    open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
    pit = PITGateway()
    pit.register_source("price_open", open_src)
    pit.register_source("price_close", close_src)

    # A pending SELL row for this idea (in flight, owned by the exit monitor).
    insert_row(conn, "orders", {
        "order_id": generate_ulid(), "dedup_hash": generate_ulid(),
        "ticker": "AAPL", "side": "SELL", "qty": 10.0,
        "horizon_bucket": "SHORT", "entry_date": str(t0.date()),
        "advisor_signature": "sig", "exits_json": "{}",
        "status": "pending", "created_at": t0.isoformat(),
        "idea_id": "M_SELLINFLIGHT",
    })

    # Past horizon → without the guard the sweep would label it "normal".
    clock = BacktestClock(t0 + timedelta(days=horizon + 2))
    result = run_outcome_sweep(conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for)

    assert result == []  # guard skipped it
    assert query_outcomes(conn, idea_id="M_SELLINFLIGHT") == []
    # Still MONITORED (the exit monitor's reconcile will close it on the fill).
    assert {i.idea_id for i in load_ideas_by_state(conn, {IdeaState.MONITORED})} == {"M_SELLINFLIGHT"}


# ---------------------------------------------------------------------------
# #5a (E2) — sweep attributes to PERSISTED OPINIONS, not the horizon proxy
# ---------------------------------------------------------------------------

def _seed_opinion(conn, *, advisor_id, ticker, stance, confidence, idea_id, as_of, fp):
    from arbiter.contract.opinion import Opinion
    from arbiter.signals import opinion_store
    from arbiter.types import ConfidenceSource

    op = Opinion(
        advisor_id=advisor_id, ticker=ticker, stance_score=stance,
        confidence=confidence, confidence_source=ConfidenceSource.MODELED,
        horizon_days=30, as_of=as_of, rationale="t",
        source_fingerprint=fp, run_group_id="rg",
    )
    opinion_store.persist_opinion(conn, op, idea_id=idea_id, as_of=as_of)


class TestRealAttribution:
    def test_sweep_uses_persisted_opinion_stance(self, conn):
        """E2: a seeded opinion makes the sweep attribute to the REAL advisor +
        stance — NOT the horizon proxy (which would have said A1.insider)."""
        t0 = _ts(2025, 7, 1)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_OP", ticker="AAPL", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        # Proxy would say A1.insider (horizon<60); the real opinion is A1.congress.
        _seed_opinion(conn, advisor_id="A1.congress", ticker="AAPL", stance=0.55,
                      confidence=0.71, idea_id="I_OP", as_of=t0, fp="fp-c")
        open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        result = run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )
        assert len(result) == 1
        rows = query_outcomes(conn, idea_id="I_OP")
        assert len(rows) == 1
        assert rows[0]["advisor_id"] == "A1.congress"  # real opinion, NOT proxy
        assert rows[0]["stance_score"] == pytest.approx(0.55)
        assert rows[0]["advisor_confidence"] == pytest.approx(0.71)
        assert {i.idea_id for i in load_ideas_by_state(conn, {IdeaState.CLOSED})} == {"I_OP"}

    def test_sweep_fans_out_two_advisors(self, conn):
        """E2 fan-out: TWO persisted opinions for one idea → TWO outcomes, each
        with its own stance; idea CLOSED only after both written."""
        t0 = _ts(2025, 8, 1)
        horizon = 30
        persist_new_idea(
            conn,
            _make_idea(idea_id="I_FAN", ticker="AAPL", horizon_days=horizon, as_of=t0),
            created_at=t0,
        )
        _seed_opinion(conn, advisor_id="A1.insider", ticker="AAPL", stance=0.9,
                      confidence=0.8, idea_id="I_FAN", as_of=t0, fp="fp-i")
        _seed_opinion(conn, advisor_id="A1.congress", ticker="AAPL", stance=0.2,
                      confidence=0.4, idea_id="I_FAN", as_of=t0, fp="fp-c")
        open_src, close_src = _build_pit_for("AAPL", t0=t0, horizon_days=horizon)
        pit = PITGateway()
        pit.register_source("price_open", open_src)
        pit.register_source("price_close", close_src)
        clock = BacktestClock(t0 + timedelta(days=horizon + 5))

        result = run_outcome_sweep(
            conn, pit=pit, clock=clock, advisor_id_for=_advisor_id_for
        )
        assert len(result) == 2
        rows = query_outcomes(conn, idea_id="I_FAN")
        by_adv = {r["advisor_id"]: r for r in rows}
        assert set(by_adv) == {"A1.insider", "A1.congress"}
        assert by_adv["A1.insider"]["stance_score"] == pytest.approx(0.9)
        assert by_adv["A1.congress"]["stance_score"] == pytest.approx(0.2)
        # Same realized alpha across both.
        assert by_adv["A1.insider"]["alpha_bps"] == pytest.approx(
            by_adv["A1.congress"]["alpha_bps"]
        )
        # CLOSED only after the full fan-out.
        assert {i.idea_id for i in load_ideas_by_state(conn, {IdeaState.CLOSED})} == {"I_FAN"}
        assert load_ideas_by_state(conn, {IdeaState.MONITORED}) == []
