-- 024_engine_state.sql — sub-project #3 (amendment C4: durable pause flag)
--
-- ``engine.paused`` is in-memory.  With the daemon's ``KeepAlive=true`` launchd
-- auto-relaunch, an auto-pause that is NOT backed by a latched breaker (e.g. a
-- broker-fatal SELL rejection that sets ``paused`` via the alerting sentinel) is
-- silently lost on crash/relaunch → the daemon would silently resume trading
-- after a fatal condition.  We persist the pause flag durably and restore it on
-- ``build_engine`` / daemon start.
--
-- This is mutable runtime state (a singleton row, id pinned to 1), like
-- ``sim_account`` — deliberately exempt from the §11.2 insert-only fact-table rule.

PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS engine_state (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    paused      INTEGER NOT NULL DEFAULT 0,   -- 0/1 boolean
    reason      TEXT NOT NULL DEFAULT '',     -- last pause reason (diagnostic)
    updated_at  TEXT NOT NULL                 -- tz-aware UTC ISO (caller's clock)
);
