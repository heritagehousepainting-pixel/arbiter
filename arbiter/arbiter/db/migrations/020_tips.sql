-- 020_tips.sql — Lane 8 (tips layer + anti-manipulation defenses)
--
-- SHADOW / DORMANT in Phase-6 MVP:
--   Tips are recorded for audit and forward-test replay but NEVER contribute
--   to live fusion.  The ``account_scores`` table persists credibility scores
--   computed by ``account_scorer.py`` so that Wave-C can back-test scoring
--   drift and eventually promote the A3 tip advisor out of shadow.
--
-- Insert-only design (INTERFACES.md §11.2):
--   All primary keys are ULIDs stored as TEXT.
--   Timestamps are tz-aware UTC ISO strings.
--   The ONLY in-place update allowed is flipping is_superseded=1.
--
-- Tables added:
--   unverified_tips      — raw tips received from adapters.
--   account_scores       — credibility scores for posting accounts.

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- unverified_tips
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS unverified_tips (
    id              TEXT PRIMARY KEY,       -- ULID
    ticker          TEXT NOT NULL,
    claim           TEXT NOT NULL,          -- raw claim text from source
    account         TEXT NOT NULL,          -- source account / handle
    source_id       TEXT NOT NULL,          -- adapter source_id (e.g. "twitter.v2")
    ts              TEXT NOT NULL,          -- information ts (tz-aware UTC ISO)
    url             TEXT NOT NULL,          -- canonical reference URL
    fingerprint     TEXT NOT NULL,          -- SHA-256 dedup key (see UnverifiedTip.fingerprint)
    supersedes_id   TEXT,
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tips_ticker_ts
    ON unverified_tips (ticker, ts);

CREATE INDEX IF NOT EXISTS idx_tips_source_id
    ON unverified_tips (source_id, ticker);

CREATE UNIQUE INDEX IF NOT EXISTS idx_tips_fingerprint
    ON unverified_tips (fingerprint)
    WHERE is_superseded = 0;

-- ---------------------------------------------------------------------------
-- account_scores
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS account_scores (
    id              TEXT PRIMARY KEY,       -- ULID
    account         TEXT NOT NULL,          -- account identifier
    source_id       TEXT NOT NULL,          -- platform the account lives on
    as_of           TEXT NOT NULL,          -- information timestamp of the score
    score           REAL NOT NULL,          -- [0.0, 1.0] credibility score
    flagged         INTEGER NOT NULL DEFAULT 0,  -- 1 = manipulator / block-listed
    reasons         TEXT,                   -- JSON array of reason strings
    supersedes_id   TEXT,
    is_superseded   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_account_scores_account_as_of
    ON account_scores (account, as_of);
