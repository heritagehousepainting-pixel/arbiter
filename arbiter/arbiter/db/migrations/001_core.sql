-- 001_core.sql  — Arbiter core schema (Lane 2)
-- Insert-only design: the ONLY in-place UPDATE allowed is flipping is_superseded=1
-- inside supersede_row() (INTERFACES.md §11.2).
-- All primary keys are ULIDs stored as TEXT.
-- All timestamps are tz-aware UTC ISO strings stored as TEXT.
-- Congress filing amounts are ALWAYS stored as (amount_low, amount_high) ranges;
-- never a midpoint (INTERFACES.md §4.3).

PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ---------------------------------------------------------------------------
-- opinions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS opinions (
    id                 TEXT PRIMARY KEY,    -- ULID
    advisor_id         TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    stance_score       REAL NOT NULL,
    confidence         REAL NOT NULL,
    confidence_source  TEXT NOT NULL,
    horizon_days       INTEGER NOT NULL,
    as_of              TEXT NOT NULL,       -- tz-aware UTC ISO string
    rationale          TEXT NOT NULL,
    source_fingerprint TEXT NOT NULL,
    run_group_id       TEXT NOT NULL,
    supersedes_id      TEXT,               -- points to previous opinion row
    is_superseded      INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_opinions_ticker_as_of
    ON opinions (ticker, as_of);

CREATE INDEX IF NOT EXISTS idx_opinions_advisor_id
    ON opinions (advisor_id);

-- ---------------------------------------------------------------------------
-- filings  (Form 4 / Congress disclosures)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS filings (
    id            TEXT PRIMARY KEY,        -- ULID
    source        TEXT NOT NULL,           -- 'form4' | 'congress'
    ticker        TEXT NOT NULL,
    person_id     TEXT NOT NULL,
    filing_ts     TEXT NOT NULL,           -- tz-aware UTC ISO string
    txn_type      TEXT NOT NULL,
    -- Congress amounts stored as low/high ranges ONLY — never a midpoint
    amount_low    REAL,
    amount_high   REAL,
    is_10b5_1     INTEGER NOT NULL DEFAULT 0,
    supersedes_id TEXT,
    is_superseded INTEGER NOT NULL DEFAULT 0,
    raw_json      TEXT,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_filings_ticker_filing_ts
    ON filings (ticker, filing_ts);

-- ---------------------------------------------------------------------------
-- ideas
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ideas (
    idea_id          TEXT PRIMARY KEY,     -- ULID
    ticker           TEXT NOT NULL,
    thesis           TEXT NOT NULL,
    horizon_days     INTEGER NOT NULL,
    state            TEXT NOT NULL,        -- IdeaState enum value
    as_of            TEXT NOT NULL,        -- original information timestamp
    -- dedupe_key stored as two columns matching Idea.dedupe_key tuple
    dedupe_key_ticker  TEXT NOT NULL,
    dedupe_key_bucket  TEXT NOT NULL,
    supersedes_id    TEXT,
    is_superseded    INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL,
    updated_state_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ideas_ticker_as_of
    ON ideas (ticker, as_of);

-- ---------------------------------------------------------------------------
-- orders
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS orders (
    order_id          TEXT PRIMARY KEY,    -- ULID
    dedup_hash        TEXT NOT NULL UNIQUE,
    ticker            TEXT NOT NULL,
    side              TEXT NOT NULL,       -- OrderSide enum value
    qty               REAL NOT NULL,
    horizon_bucket    TEXT NOT NULL,       -- HorizonBucket enum value
    entry_date        TEXT NOT NULL,       -- ISO date string
    advisor_signature TEXT NOT NULL,
    exits_json        TEXT NOT NULL,       -- JSON blob: {stop_loss, horizon_expiry, conviction_reversal}
    status            TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_orders_status
    ON orders (status);

-- ---------------------------------------------------------------------------
-- outcomes  (ResolvedOutcome)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outcomes (
    id                 TEXT PRIMARY KEY,   -- ULID
    idea_id            TEXT NOT NULL,
    advisor_id         TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    alpha_bps          REAL NOT NULL,
    binary             INTEGER NOT NULL,   -- +1 / 0 / -1
    advisor_confidence REAL NOT NULL,
    abstained          INTEGER NOT NULL,   -- 0 / 1 (bool stored as int)
    horizon_days       INTEGER NOT NULL,
    label_kind         TEXT NOT NULL,
    supersedes_id      TEXT,
    is_superseded      INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_outcomes_ticker_as_of
    ON outcomes (ticker, idea_id);

-- ---------------------------------------------------------------------------
-- trust_weights
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trust_weights (
    id            TEXT PRIMARY KEY,        -- ULID
    advisor_id    TEXT NOT NULL,
    weight        REAL NOT NULL,
    ci_low        REAL NOT NULL,
    ci_high       REAL NOT NULL,
    shadow        INTEGER NOT NULL DEFAULT 0,
    as_of         TEXT NOT NULL,           -- tz-aware UTC ISO string
    supersedes_id TEXT,
    is_superseded INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trust_weights_ticker_as_of
    ON trust_weights (advisor_id, as_of);

-- ---------------------------------------------------------------------------
-- advisor_registry
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS advisor_registry (
    advisor_id      TEXT PRIMARY KEY,
    hard_weight_cap REAL,
    registered_at   TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- breaker_state  (circuit breakers — latching, infrastructure-level)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS breaker_state (
    breaker_name TEXT PRIMARY KEY,
    latched      INTEGER NOT NULL DEFAULT 0,
    latched_at   TEXT,
    reason       TEXT
);

-- ---------------------------------------------------------------------------
-- audit_meta  (bookkeeping; actual audit lines go to audit.jsonl)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_meta (
    id         TEXT PRIMARY KEY,           -- ULID
    event      TEXT NOT NULL,
    summary    TEXT,
    created_at TEXT NOT NULL
);
