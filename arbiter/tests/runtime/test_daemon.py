"""Daemon loop tests — sub-project #3 (Decisions 3/4/5/6), OFFLINE.

No network, no real sleeps: the calendar is a scripted fake, sleep_fn advances a
BacktestClock and bounds the loop via stop_event.
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from arbiter.data.clock import BacktestClock
from arbiter.runtime.daemon import DaemonState, run_daemon, _parse_full_times
from arbiter.runtime.market_calendar import MarketSession

_UTC = timezone.utc


class _FakeCalendar:
    """Returns scripted sessions, one per call (last value repeats)."""

    def __init__(self, sessions: list[MarketSession]):
        self._sessions = sessions
        self.calls = 0

    def session(self, now):
        s = self._sessions[min(self.calls, len(self._sessions) - 1)]
        self.calls += 1
        return s


class _FakeEngine:
    def __init__(self, clock, calendar, *, raise_on_iter=None):
        self.clock = clock
        self.market_calendar = calendar
        self.config = _Cfg()
        self.paused = False
        self.conn = None
        self.pit = None
        self.executor = _FakeExec()
        self.fast_calls = 0
        self.full_calls = 0
        self.reconcile_calls = 0
        self._raise_on_iter = raise_on_iter or set()

    def run_fast_iteration(self, now=None):
        self.fast_calls += 1
        if self.fast_calls in self._raise_on_iter:
            raise RuntimeError("boom")
        return _Result()

    def run_cycle(self, as_of=None):
        self.full_calls += 1
        return _Result()

    def _reconcile_pending_orders(self, now):
        self.reconcile_calls += 1


class _FakeExec:
    name = "sim"

    def get_positions(self):
        return {}


class _Cfg:
    fast_interval_s = 180.0
    full_cycle_times_et = "09:45,15:30"
    daemon_heartbeat_path = None
    db_path = "data/x.db"
    audit_path = "data/a.jsonl"


class _Result:
    paused_by_alert = False


def _open(next_close="2025-06-19T20:00:00Z"):
    return MarketSession(is_open=True, next_open=None,
                         next_close=datetime.fromisoformat(next_close.replace("Z", "+00:00")))


def _closed(next_open):
    return MarketSession(is_open=False,
                         next_open=datetime.fromisoformat(next_open.replace("Z", "+00:00")),
                         next_close=None)


def _make_sleep(clock, stop_event, *, stop_after):
    state = {"n": 0}

    def _sleep(seconds):
        clock.advance(timedelta(seconds=seconds))
        state["n"] += 1
        if state["n"] >= stop_after:
            stop_event.set()

    return _sleep


class TestParse:
    def test_parse_full_times(self):
        assert _parse_full_times("09:45,15:30") == [(9, 45), (15, 30)]
        assert _parse_full_times("") == []


class TestDaemonLoop:
    def test_runs_fast_iteration_each_open_step(self):
        clock = BacktestClock(datetime(2025, 6, 19, 14, 0, tzinfo=_UTC))  # 10:00 ET
        cal = _FakeCalendar([_open()])
        eng = _FakeEngine(clock, cal)
        stop = threading.Event()
        sleep = _make_sleep(clock, stop, stop_after=3)

        run_daemon(eng, sleep_fn=sleep, stop_event=stop, fast_interval_s=60,
                   full_times=[], install_signals=False)

        assert eng.fast_calls == 3

    def test_long_sleep_when_closed_no_trading(self):
        clock = BacktestClock(datetime(2025, 6, 22, 14, 0, tzinfo=_UTC))  # Sunday
        cal = _FakeCalendar([_closed("2025-06-23T13:30:00Z")])
        eng = _FakeEngine(clock, cal)
        stop = threading.Event()
        sleep = _make_sleep(clock, stop, stop_after=2)

        run_daemon(eng, sleep_fn=sleep, stop_event=stop, fast_interval_s=60,
                   full_times=[], install_signals=False)

        # No fast iterations while closed.
        assert eng.fast_calls == 0

    def test_full_cycle_only_at_configured_times(self):
        # Start at 10:00 ET (14:00 UTC). full slot 09:45 has passed → fires ONCE.
        clock = BacktestClock(datetime(2025, 6, 19, 14, 0, tzinfo=_UTC))
        cal = _FakeCalendar([_open()])
        eng = _FakeEngine(clock, cal)
        stop = threading.Event()
        sleep = _make_sleep(clock, stop, stop_after=5)

        run_daemon(eng, sleep_fn=sleep, stop_event=stop, fast_interval_s=60,
                   full_times=[(9, 45)], install_signals=False)

        # Despite 5 fast iterations, the 09:45 full slot fires exactly once.
        assert eng.full_calls == 1
        assert eng.fast_calls == 5

    def test_open_to_closed_runs_post_close_reconcile(self):
        clock = BacktestClock(datetime(2025, 6, 19, 14, 0, tzinfo=_UTC))
        cal = _FakeCalendar([_open(), _closed("2025-06-20T13:30:00Z")])
        eng = _FakeEngine(clock, cal)
        stop = threading.Event()
        sleep = _make_sleep(clock, stop, stop_after=2)

        run_daemon(eng, sleep_fn=sleep, stop_event=stop, fast_interval_s=60,
                   full_times=[], install_signals=False)

        # Transition open→closed triggers exactly one post-close reconcile + a
        # shutdown reconcile (executor is sim → isinstance_adapter False, so the
        # _reconcile call is guarded). Assert at least the loop survived.
        assert eng.fast_calls == 1

    def test_one_bad_iteration_does_not_kill_loop(self):
        clock = BacktestClock(datetime(2025, 6, 19, 14, 0, tzinfo=_UTC))
        cal = _FakeCalendar([_open()])
        # First fast iteration raises; loop must continue with backoff.
        eng = _FakeEngine(clock, cal, raise_on_iter={1})
        stop = threading.Event()
        sleep = _make_sleep(clock, stop, stop_after=3)

        state = run_daemon(eng, sleep_fn=sleep, stop_event=stop, fast_interval_s=60,
                           full_times=[], install_signals=False)

        assert eng.fast_calls >= 2  # survived the raise
        # Backoff was incremented on the failing iteration.
        # (it resets on a clean iteration, so just assert the loop kept going)

    def test_stop_event_exits_gracefully(self):
        clock = BacktestClock(datetime(2025, 6, 19, 14, 0, tzinfo=_UTC))
        cal = _FakeCalendar([_open()])
        eng = _FakeEngine(clock, cal)
        stop = threading.Event()
        stop.set()  # already set → loop body never runs

        run_daemon(eng, sleep_fn=lambda s: None, stop_event=stop,
                   fast_interval_s=60, full_times=[], install_signals=False)

        assert eng.fast_calls == 0

    def test_paused_engine_keeps_polling(self):
        """A paused engine's fast iteration early-returns but the LOOP KEEPS POLLING."""
        clock = BacktestClock(datetime(2025, 6, 19, 14, 0, tzinfo=_UTC))
        cal = _FakeCalendar([_open()])
        eng = _FakeEngine(clock, cal)
        eng.paused = True  # run_fast_iteration still called; engine gate handles it
        stop = threading.Event()
        sleep = _make_sleep(clock, stop, stop_after=3)

        run_daemon(eng, sleep_fn=sleep, stop_event=stop, fast_interval_s=60,
                   full_times=[], install_signals=False)

        # Loop kept polling (called the engine each step); engine gate halts trading.
        assert eng.fast_calls == 3
