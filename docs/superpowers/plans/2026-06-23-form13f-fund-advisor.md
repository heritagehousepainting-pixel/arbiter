# Form 13F Fund-Manager Advisor (`A1.fund`) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a probationary `A1.fund` advisor that turns famous fund managers' quarterly SEC Form 13F-HR holdings into delta-based (quarter-over-quarter) trading signals, expanding arbiter's tracked smart money.

**Architecture:** Reuse the existing disclosure pipeline. A CIK-driven 13F ingest fetches each tracked manager's 13F-HR information table, stores raw holdings, diffs against the prior quarter, and writes each meaningful change as a `filings` row with `source="form13f"` (free-TEXT source, no enum migration — exactly how `form13d` was added). From there `detect_signals` → `emit_opinion` → a new `_build_a1_fund_fn` advisor flow it through the council, governed by the learning loop. PIT-safe (`as_of` = filing date) and inert under `BacktestClock`.

**Tech Stack:** Python 3 (`.venv/bin/python`), sqlite3, httpx (via existing `EdgarClient`), structlog, pytest. No new third-party dependencies.

**Spec:** `docs/specs/2026-06-23-form13f-fund-advisor-spec.md` (read it first).

## Global Constraints

- **Interpreter:** always `arbiter/.venv/bin/python`. Package is NESTED at `arbiter/arbiter/`; run pytest from `arbiter/`.
- **Test gate (hermetic):** `KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ -q` must stay green (~2441 baseline, growing).
- **Linters (both must stay clean):** `bash scripts/check_no_lookahead.sh` and `bash scripts/check_insert_only.sh`.
- **No version control:** this project is NOT a git repo. "Checkpoint" steps run the relevant tests instead of `git commit`. Do not run `git`.
- **No `datetime.now()`** in signal/ingest code — callers pass `as_of`/`clock.now()` (no-lookahead lint enforces this).
- **Insert-only store:** the only permitted UPDATE is `is_superseded` via `supersede_rows` / the documented lifecycle carve-outs. New tables are insert-only except the `cusip_map` cache (additive upsert) and `form13f_holdings` (insert-only).
- **PIT rule:** every `form13f` signal's `as_of`/`filing_ts` is the EDGAR **filing date**, never the 13F `report_date` (quarter-end).
- **`source="form13f"`** is the discriminator everywhere; advisor id is **`A1.fund`**; horizon **180 days (LONG)**; conviction **hard-capped at 0.7**.
- **Config defaults (verbatim):** `FORM13F_MIN_POSITION_USD=10_000_000`, `FORM13F_MIN_BOOK_FRACTION=0.005`, `FORM13F_MIN_DELTA_FRACTION=0.25`, `FORM13F_FIRST_FILING_TOP_K=5`, `FORM13F_MAX_CONVICTION=0.7`.
- **Safety:** never trade an unresolved/low-confidence CUSIP — drop and log.

---

## File Structure

**Create:**
- `arbiter/arbiter/db/migrations/027_form13f_holdings.sql` — raw quarterly snapshots.
- `arbiter/arbiter/db/migrations/028_cusip_map.sql` — cached CUSIP→ticker resolutions.
- `arbiter/arbiter/data/fund_managers.py` — static roster seed `[(name, fund, cik10)]`.
- `arbiter/arbiter/ingest/edgar/cusip_resolver.py` — CUSIP→ticker resolve + cache + drop.
- `arbiter/arbiter/ingest/edgar/form13f_parser.py` — parse the 13F information-table XML.
- `arbiter/arbiter/ingest/edgar/form13f_normalize.py` — store holdings + delta engine → `RawFiling` rows.

**Modify:**
- `arbiter/arbiter/config.py` — add `FORM13F_*` keys.
- `arbiter/arbiter/ingest/edgar/client.py` — `search_form13f_filings`, `get_form13f_info_table`.
- `arbiter/arbiter/ingest/runner.py` — `_ingest_form13f`, register managers as `people`, wire `"form13f"` source.
- `arbiter/arbiter/signals/detection.py` — `_detect_fund_holdings` (reads `source="form13f"`).
- `arbiter/arbiter/signals/emit.py` — `source=="form13f"` → `A1.fund`, 180d, sign flip on `'S'`.
- `arbiter/arbiter/engine/advisors.py` — `_build_a1_fund_fn`.
- `arbiter/arbiter/engine/__init__.py` — export `_build_a1_fund_fn`.
- `arbiter/arbiter/engine/_engine.py` — register `A1.fund` in `advisor_map`; add `"form13f"` to the 180d horizon set (~line 527).
- `cockpit/api/graph.py` (or wherever the advisor nodes are defined) — add/un-dim the `A1.fund` node.

**Test (create):**
- `tests/ingest/edgar/test_cusip_resolver.py`
- `tests/ingest/edgar/test_form13f_parser.py`
- `tests/ingest/edgar/test_form13f_normalize.py`
- `tests/ingest/edgar/test_form13f_client.py`
- `tests/ingest/test_runner_form13f.py`
- `tests/signals/test_detection_form13f.py`
- `tests/signals/test_emit_form13f.py`
- `tests/engine/test_a1_fund_advisor.py`
- `tests/fixtures/form13f_infotable_sample.xml` (real-shaped fixture)

---

## Task 1: Schema migrations (holdings + cusip cache)

**Files:**
- Create: `arbiter/arbiter/db/migrations/027_form13f_holdings.sql`
- Create: `arbiter/arbiter/db/migrations/028_cusip_map.sql`
- Test: `tests/ingest/edgar/test_form13f_normalize.py` (schema portion)

**Interfaces:**
- Produces: tables `form13f_holdings(person_id, accession, filing_date, report_date, cusip, ticker, issuer_name, value_usd, shares, put_call, created_at)` with `UNIQUE(person_id, accession, cusip, put_call)`; `cusip_map(cusip PRIMARY KEY, ticker, issuer_name, source, confidence, resolved_at)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingest/edgar/test_form13f_normalize.py
import sqlite3
from arbiter.db.migrate import apply_migrations  # existing migration runner

def _migrated_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    apply_migrations(conn)
    return conn

def test_migrations_create_form13f_tables():
    conn = _migrated_conn()
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(form13f_holdings)")}
    assert {"person_id", "accession", "filing_date", "report_date", "cusip",
            "ticker", "issuer_name", "value_usd", "shares", "put_call"} <= cols
    map_cols = {r["name"] for r in conn.execute("PRAGMA table_info(cusip_map)")}
    assert {"cusip", "ticker", "confidence"} <= map_cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py::test_migrations_create_form13f_tables -v`
Expected: FAIL — `no such table: form13f_holdings` (confirm `apply_migrations` import path first; if the runner is named differently, grep `db/migrate.py` and adjust the import).

- [ ] **Step 3: Write migration 027**

```sql
-- arbiter/arbiter/db/migrations/027_form13f_holdings.sql
-- Raw quarterly 13F-HR holdings snapshots (insert-only). Diffed quarter-over-
-- quarter by form13f_normalize.py to produce form13f filing rows.
CREATE TABLE IF NOT EXISTS form13f_holdings (
    id           TEXT PRIMARY KEY,           -- ULID
    person_id    TEXT NOT NULL,              -- manager (FK people.person_id)
    accession    TEXT NOT NULL,              -- EDGAR accession (idempotency)
    filing_date  TEXT NOT NULL,              -- tz-aware ISO; PIT as_of source
    report_date  TEXT NOT NULL,              -- quarter-end the snapshot describes
    cusip        TEXT NOT NULL,
    ticker       TEXT,                        -- nullable when CUSIP unresolved
    issuer_name  TEXT,
    value_usd    REAL NOT NULL DEFAULT 0,
    shares       REAL NOT NULL DEFAULT 0,
    put_call     TEXT,                        -- NULL = outright shares; 'Put'/'Call' otherwise
    created_at   TEXT NOT NULL,
    UNIQUE(person_id, accession, cusip, put_call)
);
CREATE INDEX IF NOT EXISTS idx_form13f_holdings_person_report
    ON form13f_holdings(person_id, report_date);
```

- [ ] **Step 4: Write migration 028**

```sql
-- arbiter/arbiter/db/migrations/028_cusip_map.sql
-- Cached CUSIP -> ticker resolutions (additive upsert cache, NOT trade state).
CREATE TABLE IF NOT EXISTS cusip_map (
    cusip       TEXT PRIMARY KEY,
    ticker      TEXT NOT NULL,
    issuer_name TEXT,
    source      TEXT NOT NULL,   -- 'seed' | 'alpaca_name' | 'manual'
    confidence  REAL NOT NULL,   -- [0,1]; only >= 0.9 are trusted for trading
    resolved_at TEXT NOT NULL
);
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py::test_migrations_create_form13f_tables -v`
Expected: PASS

- [ ] **Step 6: Checkpoint — confirm `check_insert_only.sh` still clean**

Run: `cd arbiter && bash scripts/check_insert_only.sh`
Expected: clean (new tables are insert-only; the cusip_map upsert is added in Task 4 and must be whitelisted there if the linter flags it).

---

## Task 2: Config keys

**Files:**
- Modify: `arbiter/arbiter/config.py`
- Test: `tests/ingest/edgar/test_form13f_normalize.py` (config portion)

**Interfaces:**
- Produces: `Config` gains float/int attributes `form13f_min_position_usd`, `form13f_min_book_fraction`, `form13f_min_delta_fraction`, `form13f_first_filing_top_k`, `form13f_max_conviction`, and optional `form13f_manager_ciks: tuple[str, ...] | None` (env override of the roster).

- [ ] **Step 1: Write the failing test**

```python
def test_config_form13f_defaults(monkeypatch):
    from arbiter.config import Config
    cfg = Config.load()  # use the project's actual constructor/loader
    assert cfg.form13f_min_position_usd == 10_000_000
    assert cfg.form13f_min_book_fraction == 0.005
    assert cfg.form13f_min_delta_fraction == 0.25
    assert cfg.form13f_first_filing_top_k == 5
    assert cfg.form13f_max_conviction == 0.7
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py::test_config_form13f_defaults -v`
Expected: FAIL — `AttributeError: form13f_min_position_usd`. (First grep `config.py` for how other tunables like the A3 `a3_min_stance` key are declared + env-loaded, and mirror that exact pattern — field declaration, env var name `FORM13F_*`, default, and `_env_float`/`_env_int` helper.)

- [ ] **Step 3: Add the config fields**

Mirror the existing `A3_MIN_STANCE` pattern. Add fields with env overrides:
`FORM13F_MIN_POSITION_USD` (float, 10_000_000), `FORM13F_MIN_BOOK_FRACTION` (float, 0.005), `FORM13F_MIN_DELTA_FRACTION` (float, 0.25), `FORM13F_FIRST_FILING_TOP_K` (int, 5), `FORM13F_MAX_CONVICTION` (float, 0.7), `FORM13F_MANAGER_CIKS` (comma-split → tuple or None).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py::test_config_form13f_defaults -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/test_config.py -q` (or the config test module)
Expected: PASS

---

## Task 3: Manager roster seed

**Files:**
- Create: `arbiter/arbiter/data/fund_managers.py`
- Test: `tests/ingest/test_runner_form13f.py` (roster portion)

**Interfaces:**
- Produces: `FUND_MANAGERS: tuple[FundManager, ...]` where `FundManager = namedtuple/dataclass(name: str, fund: str, cik: str)` with `cik` a 10-digit zero-padded string; helper `manager_ciks() -> tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingest/test_runner_form13f.py
import re
from arbiter.data.fund_managers import FUND_MANAGERS, manager_ciks

def test_roster_shape_and_ciks():
    assert len(FUND_MANAGERS) >= 11
    names = {m.name for m in FUND_MANAGERS}
    assert "Leopold Aschenbrenner" in names
    assert "Cathie Wood" in names and "Michael Burry" in names
    for m in FUND_MANAGERS:
        assert re.fullmatch(r"\d{10}", m.cik), f"{m.name} cik not 10-digit: {m.cik}"
    assert set(manager_ciks()) == {m.cik for m in FUND_MANAGERS}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/test_runner_form13f.py::test_roster_shape_and_ciks -v`
Expected: FAIL — `ModuleNotFoundError: arbiter.data.fund_managers`.

- [ ] **Step 3: Resolve each manager's CIK live, then write the seed**

The 13F **filer** CIK (the management company, not the person) must be looked up and **verified** — do NOT fabricate. For each fund, fetch EDGAR full-text/company search and confirm the entity files 13F-HR. Command per manager (example):

```bash
cd arbiter && .venv/bin/python -c "
import httpx
ua='Jonathan Morris heritagehousepainting@gmail.com'
# EDGAR company search JSON by name fragment:
r=httpx.get('https://www.sec.gov/cgi-bin/browse-edgar',
            params={'action':'getcompany','company':'ARK INVESTMENT MANAGEMENT',
                    'type':'13F-HR','dateb':'','owner':'include','count':'10','output':'atom'},
            headers={'User-Agent':ua}, timeout=30, follow_redirects=True)
print(r.text[:2000])
"
```

Then write the seed (CIK values below are PLACEHOLDERS to replace with the verified 10-digit CIKs):

```python
# arbiter/arbiter/data/fund_managers.py
"""Static roster of tracked 13F fund managers (the A1.fund universe).

Each entry maps a famous manager to their 13F FILER cik (the management
company that files 13F-HR, NOT the natural person).  Extending the roster
is one line.  CIKs are verified against EDGAR at authoring time.
"""
from __future__ import annotations
from typing import NamedTuple

class FundManager(NamedTuple):
    name: str   # canonical person name (for the people table / cockpit)
    fund: str   # filer entity name
    cik: str    # 10-digit zero-padded EDGAR CIK of the 13F filer

FUND_MANAGERS: tuple[FundManager, ...] = (
    FundManager("Cathie Wood", "ARK Investment Management LLC", "0001697748"),  # VERIFY
    FundManager("Michael Burry", "Scion Asset Management LLC", "0001649339"),   # VERIFY
    FundManager("Warren Buffett", "Berkshire Hathaway Inc", "0001067983"),       # VERIFY
    FundManager("Bill Ackman", "Pershing Square Capital Management", "0001336528"),  # VERIFY
    FundManager("David Tepper", "Appaloosa LP", "0001656456"),                   # VERIFY
    FundManager("David Einhorn", "Greenlight Capital Inc", "0001079114"),        # VERIFY
    FundManager("Stanley Druckenmiller", "Duquesne Family Office LLC", "0001536411"),  # VERIFY
    FundManager("Seth Klarman", "Baupost Group LLC", "0001061768"),              # VERIFY
    FundManager("Chase Coleman", "Tiger Global Management LLC", "0001167483"),   # VERIFY
    FundManager("Daniel Loeb", "Third Point LLC", "0001040273"),                 # VERIFY
    FundManager("Leopold Aschenbrenner", "Situational Awareness LP", "0000000000"),  # VERIFY (may not file yet)
)

def manager_ciks() -> tuple[str, ...]:
    return tuple(m.cik for m in FUND_MANAGERS)
```

> NOTE: replace every `# VERIFY` CIK with the value confirmed from EDGAR before this task is considered done. If a manager has no 13F filer entity yet (e.g. Aschenbrenner), keep the row with the best-known CIK or a clearly-marked sentinel and the feed will simply find no filings.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/test_runner_form13f.py::test_roster_shape_and_ciks -v`
Expected: PASS

- [ ] **Step 5: Checkpoint — live CIK verification (manual)**

For at least Wood, Burry, Buffett: confirm the seeded CIK's submissions JSON contains `13F-HR` forms:

```bash
cd arbiter && .venv/bin/python -c "
import httpx, json
ua='Jonathan Morris heritagehousepainting@gmail.com'
for cik in ['0001697748','0001649339','0001067983']:
    b=httpx.get(f'https://data.sec.gov/submissions/CIK{cik}.json',headers={'User-Agent':ua},timeout=30).json()
    forms=set(b['filings']['recent']['form'])
    print(cik, b.get('name'), '13F-HR' in forms)
"
```
Expected: each prints the fund name and `True`. Fix any wrong CIK.

---

## Task 4: CUSIP→ticker resolver

**Files:**
- Create: `arbiter/arbiter/ingest/edgar/cusip_resolver.py`
- Test: `tests/ingest/edgar/test_cusip_resolver.py`

**Interfaces:**
- Consumes: a sqlite `conn`; an injectable `asset_lookup: Callable[[], dict[str, str]]` returning `{issuer_name_upper: ticker}` (Alpaca US-equity assets); the `cusip_map` table (Task 1).
- Produces: `resolve_cusip(conn, cusip, issuer_name, *, asset_lookup, now_iso) -> str | None` (returns a tradeable ticker or `None` to drop). Caches confident resolutions. `_SEED: dict[str, str]` of megacap CUSIP→ticker.

- [ ] **Step 1: Write the failing test**

```python
# tests/ingest/edgar/test_cusip_resolver.py
import sqlite3
from arbiter.db.migrate import apply_migrations
from arbiter.ingest.edgar import cusip_resolver as cr

def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row
    apply_migrations(c); return c

NOW = "2026-06-23T00:00:00+00:00"
ASSETS = {"NVIDIA CORP": "NVDA", "APPLE INC": "AAPL"}

def test_resolve_via_seed():
    c = _conn()
    # 67066G104 = NVDA, in the megacap seed
    assert cr.resolve_cusip(c, "67066G104", "NVIDIA CORP", asset_lookup=lambda: ASSETS, now_iso=NOW) == "NVDA"

def test_resolve_via_exact_name_match_and_caches():
    c = _conn()
    t = cr.resolve_cusip(c, "999999999", "APPLE INC", asset_lookup=lambda: ASSETS, now_iso=NOW)
    assert t == "AAPL"
    row = c.execute("SELECT ticker, confidence FROM cusip_map WHERE cusip='999999999'").fetchone()
    assert row["ticker"] == "AAPL" and row["confidence"] >= 0.9

def test_drops_unresolvable():
    c = _conn()
    assert cr.resolve_cusip(c, "111111111", "OBSCURE FOREIGN HOLDINGS PLC",
                            asset_lookup=lambda: ASSETS, now_iso=NOW) is None

def test_cache_hit_short_circuits(monkeypatch):
    c = _conn()
    c.execute("INSERT INTO cusip_map VALUES (?,?,?,?,?,?)",
              ("222", "TSLA", "TESLA INC", "manual", 1.0, NOW)); c.commit()
    called = {"n": 0}
    def boom():
        called["n"] += 1; return {}
    assert cr.resolve_cusip(c, "222", "WHATEVER", asset_lookup=boom, now_iso=NOW) == "TSLA"
    assert called["n"] == 0  # cache hit never consults the asset list
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_cusip_resolver.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement the resolver**

```python
# arbiter/arbiter/ingest/edgar/cusip_resolver.py
"""Resolve a 13F CUSIP to a tradeable US-equity ticker, safety-first.

Order: cusip_map cache -> megacap seed -> exact issuer-name match against the
Alpaca tradeable US-equity asset list.  Anything not resolved with high
confidence is DROPPED (returns None) and never traded.  Confident resolutions
are cached in cusip_map so the map grows.
"""
from __future__ import annotations
import sqlite3
from typing import Callable
import structlog
from arbiter.db.helpers import generate_ulid  # noqa: F401  (ULID not needed; cusip is PK)

log = structlog.get_logger(__name__)

_TRUST = 0.9  # min confidence trusted for trading

# Hand-seeded megacap CUSIP -> ticker (verified). Extend as needed.
_SEED: dict[str, str] = {
    "67066G104": "NVDA",  # NVIDIA
    "037833100": "AAPL",  # Apple
    "023135106": "AMZN",  # Amazon
    "594918104": "MSFT",  # Microsoft
    "88160R101": "TSLA",  # Tesla
    "02079K305": "GOOGL", # Alphabet A
    "30303M102": "META",  # Meta
}

def _cache_get(conn: sqlite3.Connection, cusip: str) -> str | None:
    row = conn.execute(
        "SELECT ticker, confidence FROM cusip_map WHERE cusip = ?", (cusip,)
    ).fetchone()
    if row and row["confidence"] >= _TRUST:
        return row["ticker"]
    return None

def _cache_put(conn, cusip, ticker, issuer_name, source, confidence, now_iso):
    conn.execute(
        "INSERT OR REPLACE INTO cusip_map "
        "(cusip, ticker, issuer_name, source, confidence, resolved_at) "
        "VALUES (?,?,?,?,?,?)",
        (cusip, ticker, issuer_name, source, confidence, now_iso),
    )
    conn.commit()

def resolve_cusip(
    conn: sqlite3.Connection,
    cusip: str,
    issuer_name: str,
    *,
    asset_lookup: Callable[[], dict[str, str]],
    now_iso: str,
) -> str | None:
    cusip = (cusip or "").strip().upper()
    if not cusip:
        return None
    # 1. cache
    cached = _cache_get(conn, cusip)
    if cached:
        return cached
    # 2. seed
    if cusip in _SEED:
        t = _SEED[cusip]
        _cache_put(conn, cusip, t, issuer_name, "seed", 1.0, now_iso)
        return t
    # 3. exact issuer-name match against tradeable assets
    name = (issuer_name or "").strip().upper()
    if name:
        assets = asset_lookup() or {}
        t = assets.get(name)
        if t:
            _cache_put(conn, cusip, t, issuer_name, "alpaca_name", 0.9, now_iso)
            return t
    log.info("cusip.unresolved", cusip=cusip, issuer=issuer_name)
    return None  # drop, never guess
```

> If `check_insert_only.sh` flags the `INSERT OR REPLACE` on `cusip_map`, add `cusip_map` to its allowlist (it is a cache, not trade state) — mirror how the linter already exempts non-ledger tables.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_cusip_resolver.py -v`
Expected: PASS (4 tests)

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && bash scripts/check_insert_only.sh && bash scripts/check_no_lookahead.sh`
Expected: both clean.

---

## Task 5: 13F information-table parser

**Files:**
- Create: `arbiter/arbiter/ingest/edgar/form13f_parser.py`
- Create: `tests/fixtures/form13f_infotable_sample.xml`
- Test: `tests/ingest/edgar/test_form13f_parser.py`

**Interfaces:**
- Produces: `parse_form13f_infotable(xml: str) -> list[dict]` where each dict = `{"cusip": str, "issuer_name": str, "value_usd": float, "shares": float, "put_call": str | None}`. Never raises on hostile/empty input (returns `[]`), matching the other EDGAR parsers.

- [ ] **Step 1: Create the fixture**

A real 13F info table is namespaced XML. Save a representative `tests/fixtures/form13f_infotable_sample.xml`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>NVIDIA CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>67066G104</cusip>
    <value>1500000</value>
    <shrsOrPrnAmt><sshPrnamt>10000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>10000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>CALL</titleOfClass>
    <cusip>037833100</cusip>
    <value>500000</value>
    <shrsOrPrnAmt><sshPrnamt>2000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>Call</putCall>
    <investmentDiscretion>SOLE</investmentDiscretion>
  </infoTable>
</informationTable>
```

> NOTE: 13F `<value>` historically meant **thousands of dollars** for pre-2023 filings and **whole dollars** from 2023 onward. Treat `<value>` as **whole dollars** (post-2023 standard, which is all our managers file now). Document this assumption in the parser docstring.

- [ ] **Step 2: Write the failing test**

```python
# tests/ingest/edgar/test_form13f_parser.py
from pathlib import Path
from arbiter.ingest.edgar.form13f_parser import parse_form13f_infotable

FIX = Path(__file__).parents[2] / "fixtures" / "form13f_infotable_sample.xml"

def test_parses_holdings():
    rows = parse_form13f_infotable(FIX.read_text())
    assert len(rows) == 2
    nv = next(r for r in rows if r["cusip"] == "67066G104")
    assert nv["issuer_name"] == "NVIDIA CORP"
    assert nv["value_usd"] == 1_500_000.0
    assert nv["shares"] == 10_000.0
    assert nv["put_call"] is None
    ap = next(r for r in rows if r["cusip"] == "037833100")
    assert ap["put_call"] == "Call"

def test_malformed_never_raises():
    assert parse_form13f_infotable("") == []
    assert parse_form13f_infotable("<not><closed>") == []
    assert parse_form13f_infotable("<informationTable></informationTable>") == []
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_parser.py -v`
Expected: FAIL — module not found.

- [ ] **Step 4: Implement the parser**

```python
# arbiter/arbiter/ingest/edgar/form13f_parser.py
"""Parse a 13F-HR information-table XML into holding dicts.

Namespaced XML (http://www.sec.gov/edgar/document/thirteenf/informationtable).
<value> is treated as WHOLE DOLLARS (SEC standard since 2023).  Never raises on
hostile/empty input -> returns [] (parity with the form4/sc13 parsers).
"""
from __future__ import annotations
import xml.etree.ElementTree as ET
import structlog

log = structlog.get_logger(__name__)

def _localname(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag

def _find_text(el, local: str) -> str | None:
    for child in el.iter():
        if _localname(child.tag) == local and child.text:
            return child.text.strip()
    return None

def parse_form13f_infotable(xml: str) -> list[dict]:
    if not xml or not xml.strip():
        return []
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        log.warning("form13f.parse_error")
        return []
    out: list[dict] = []
    for el in root.iter():
        if _localname(el.tag) != "infoTable":
            continue
        cusip = _find_text(el, "cusip")
        if not cusip:
            continue
        try:
            value_usd = float(_find_text(el, "value") or 0)
            shares = float(_find_text(el, "sshPrnamt") or 0)
        except ValueError:
            continue
        out.append({
            "cusip": cusip.strip().upper(),
            "issuer_name": (_find_text(el, "nameOfIssuer") or "").strip(),
            "value_usd": value_usd,
            "shares": shares,
            "put_call": (_find_text(el, "putCall") or None),
        })
    return out
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_parser.py -v`
Expected: PASS (2 tests)

- [ ] **Step 6: Checkpoint**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/ -q`
Expected: PASS

---

## Task 6: EdgarClient — discover + fetch 13F filings

**Files:**
- Modify: `arbiter/arbiter/ingest/edgar/client.py`
- Test: `tests/ingest/edgar/test_form13f_client.py`

**Interfaces:**
- Consumes: existing `EdgarClient._get`, `_parse_submissions_json`, `_SUBMISSIONS_URL_TMPL`.
- Produces:
  - `search_form13f_filings(self, cik: str, *, count: int = 8) -> list[dict]` — newest-first dicts `{"cik","accession","filed_at","report_date","primary_document","is_amendment"}` for `13F-HR`/`13F-HR/A`. Uses the MANAGER's own CIK directly (no ticker→CIK lookup).
  - `get_form13f_info_table(self, accession: str, cik: str) -> str` — locate + fetch the information-table XML by scraping the filing index for the doc whose name contains `infotable`/`form13f` or `.xml` with `<informationTable>`.

- [ ] **Step 1: Write the failing test** (use the project's existing EDGAR fake-HTTP harness — grep `tests/ingest/edgar/` for the fake `_get`/transport pattern and reuse it)

```python
# tests/ingest/edgar/test_form13f_client.py
from arbiter.ingest.edgar.client import EdgarClient
from arbiter.config import Config

# Reuse the repo's existing EdgarClient test seam. Many edgar tests monkeypatch
# EdgarClient._get to return canned bodies keyed by URL; copy that helper here.
SUBMISSIONS = """{"cik":"1697748","name":"ARK INVESTMENT MANAGEMENT LLC",
"filings":{"recent":{
  "form":["13F-HR","4","13F-HR/A"],
  "accessionNumber":["0001697748-26-000010","0000000000-26-000001","0001697748-26-000005"],
  "filingDate":["2026-05-15","2026-05-01","2026-02-14"],
  "reportDate":["2026-03-31","","2025-12-31"],
  "primaryDocument":["primary_doc.xml","x","primary_doc.xml"]}}}"""

def test_search_form13f_filings(monkeypatch):
    cfg = Config.load()
    c = EdgarClient(config=cfg) if cfg.edgar_user_agent else EdgarClient.__new__(EdgarClient)
    monkeypatch.setattr(c, "_get", lambda url, **k: SUBMISSIONS)
    rows = c.search_form13f_filings("0001697748", count=8)
    assert {r["accession"] for r in rows} == {"0001697748-26-000010", "0001697748-26-000005"}
    hr = next(r for r in rows if r["accession"] == "0001697748-26-000010")
    assert hr["report_date"] == "2026-03-31" and hr["is_amendment"] is False
    amd = next(r for r in rows if r["accession"] == "0001697748-26-000005")
    assert amd["is_amendment"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_client.py -v`
Expected: FAIL — `AttributeError: search_form13f_filings`.

- [ ] **Step 3: Implement the two methods**

Add near `search_sc13_filings`. `search_form13f_filings` calls `_parse_submissions_json(body, cik, form_types={"13F-HR","13F-HR/A"}, count=count, keep_form=True)`, then sets `is_amendment = form.endswith("/A")` and forwards `report_date` (pull it from submissions `reportDate` — extend `_parse_submissions_json` to carry `reportDate` if it doesn't already; grep it first). `get_form13f_info_table` scrapes the accession index (reuse `_fetch_primary_doc`'s index-scrape helper) to find the information-table document:

```python
def search_form13f_filings(self, cik: str, *, count: int = 8) -> list[dict]:
    """Discover a MANAGER's own recent 13F-HR filings via their submissions JSON.

    ``cik`` is the manager's filer CIK (NOT a ticker).  Returns newest-first
    dicts with cik/accession/filed_at/report_date/primary_document/is_amendment.
    """
    from arbiter.ingest.edgar.client import _SUBMISSIONS_URL_TMPL  # noqa: PLC0415
    body = self._get(_SUBMISSIONS_URL_TMPL.format(cik10=cik))
    rows = _parse_submissions_json(
        body, cik, form_types={"13F-HR", "13F-HR/A"}, count=count, keep_form=True
    )
    for row in rows:
        form = row.pop("form", "")
        row["is_amendment"] = form.endswith("/A")
    return rows

def get_form13f_info_table(self, accession: str, cik: str) -> str:
    """Fetch the 13F information-table XML for a filing.

    The 13F primary_document is the cover page; holdings live in a SEPARATE
    information-table XML.  Scrape the filing index for the document whose name
    contains 'infotable' or 'form13f' (case-insensitive) and ends in .xml.
    """
    # _list_filing_documents(accession, cik) -> list[str] of doc filenames.
    # If a helper like this does not exist, factor the index-scrape out of
    # _fetch_primary_doc; otherwise reuse it.
    docs = self._list_filing_documents(accession, cik)
    candidates = [d for d in docs
                  if d.lower().endswith(".xml")
                  and ("infotable" in d.lower() or "form13f" in d.lower())]
    target = candidates[0] if candidates else next(
        (d for d in docs if d.lower().endswith(".xml") and "primary_doc" not in d.lower()),
        None,
    )
    if target is None:
        return ""
    return self._fetch_document(accession, cik, target)
```

> The exact index-scrape/document-fetch primitives (`_list_filing_documents`, `_fetch_document`) may need to be factored out of the existing `_fetch_primary_doc`. Read that method first and reuse its index-scrape + URL-build logic rather than duplicating it.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_client.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/ -q`
Expected: PASS

---

## Task 7: Holdings store + delta engine (the core)

**Files:**
- Create: `arbiter/arbiter/ingest/edgar/form13f_normalize.py`
- Test: `tests/ingest/edgar/test_form13f_normalize.py` (delta portion)

**Interfaces:**
- Consumes: `form13f_holdings`/`cusip_map` (Task 1), `resolve_cusip` (Task 4), `RawFiling` TypedDict (from `ingest/edgar/normalize.py`), `Config` (Task 2).
- Produces:
  - `store_holdings(conn, person_id, accession, filing_date, report_date, holdings, *, asset_lookup, now_iso) -> int` — resolves CUSIPs, inserts resolvable outright-share rows into `form13f_holdings` (idempotent), returns count stored.
  - `compute_deltas(conn, person_id, report_date, *, config) -> list[RawFiling]` — diffs this report_date's snapshot vs the manager's most recent PRIOR report_date and returns `form13f` `RawFiling` rows (`txn_type` `"P"`/`"S"`). First-filing → top-K conviction snapshot.

- [ ] **Step 1: Write the failing tests**

```python
# tests/ingest/edgar/test_form13f_normalize.py (append)
from arbiter.config import Config
from arbiter.ingest.edgar import form13f_normalize as fn

ASSETS = {"NVIDIA CORP": "NVDA", "APPLE INC": "AAPL", "TESLA INC": "TSLA",
          "AMAZON COM INC": "AMZN", "META PLATFORMS INC": "META", "MICROSOFT CORP": "MSFT"}
NOW = "2026-06-23T00:00:00+00:00"

def _store(c, pid, acc, fdate, rdate, holdings):
    return fn.store_holdings(c, pid, acc, fdate, rdate, holdings,
                             asset_lookup=lambda: ASSETS, now_iso=NOW)

def _h(name, cusip, value, shares, put_call=None):
    return {"issuer_name": name, "cusip": cusip, "value_usd": value,
            "shares": shares, "put_call": put_call}

def test_first_filing_emits_top_k_conviction_snapshot():
    c = _migrated_conn()
    cfg = Config.load()
    # 6 holdings of descending value; top-5 should fire as new "P".
    hs = [_h("NVIDIA CORP","67066G104",60e6,1000), _h("APPLE INC","037833100",50e6,1000),
          _h("TESLA INC","88160R101",40e6,1000), _h("AMAZON COM INC","023135106",30e6,1000),
          _h("META PLATFORMS INC","30303M102",20e6,1000), _h("MICROSOFT CORP","594918104",11e6,1000)]
    _store(c, "p1", "acc1", "2026-05-15T00:00:00+00:00", "2026-03-31", hs)
    deltas = fn.compute_deltas(c, "p1", "2026-03-31", config=cfg)
    assert len(deltas) == 5  # top-5 only
    assert all(d["txn_type"] == "P" and d["source"] == "form13f" for d in deltas)
    # PIT: filing_ts is the filing date, never the report_date.
    assert all(d["filing_ts"].startswith("2026-05-15") for d in deltas)

def test_new_exit_add_trim_flat():
    c = _migrated_conn(); cfg = Config.load()
    q1 = [_h("NVIDIA CORP","67066G104",60e6,1000), _h("APPLE INC","037833100",60e6,1000),
          _h("TESLA INC","88160R101",60e6,1000)]
    _store(c, "p1","a1","2026-02-14T00:00:00+00:00","2025-12-31", q1)
    fn.compute_deltas(c, "p1","2025-12-31", config=cfg)  # baseline already top-5 (all 3)
    q2 = [
        _h("NVIDIA CORP","67066G104",60e6,1000),        # flat -> no signal
        _h("APPLE INC","037833100",60e6,2000),          # +100% add -> P
        _h("TESLA INC","88160R101",60e6,400),           # -60% trim -> S
        _h("AMAZON COM INC","023135106",60e6,1000),     # new -> P
    ]                                                    # (META exited fully -> none here)
    _store(c, "p1","a2","2026-05-15T00:00:00+00:00","2026-03-31", q2)
    deltas = {d["ticker"]: d for d in fn.compute_deltas(c, "p1","2026-03-31", config=cfg)}
    assert "NVDA" not in deltas                          # flat
    assert deltas["AAPL"]["txn_type"] == "P"
    assert deltas["TSLA"]["txn_type"] == "S"
    assert deltas["AMZN"]["txn_type"] == "P"

def test_noise_floors_drop_small_positions():
    c = _migrated_conn(); cfg = Config.load()
    # value below FORM13F_MIN_POSITION_USD ($10M) -> not stored as a tradeable delta.
    _store(c, "p1","a1","2026-05-15T00:00:00+00:00","2026-03-31",
           [_h("APPLE INC","037833100",5e6,10)])
    assert fn.compute_deltas(c, "p1","2026-03-31", config=cfg) == []

def test_unresolved_cusip_dropped():
    c = _migrated_conn(); cfg = Config.load()
    n = _store(c, "p1","a1","2026-05-15T00:00:00+00:00","2026-03-31",
               [_h("OBSCURE PLC","ZZZ999999",60e6,1000)])
    assert n == 0  # unresolved -> not stored
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py -v -k "first_filing or new_exit or noise or unresolved"`
Expected: FAIL — module/functions not found.

- [ ] **Step 3: Implement store + delta engine**

```python
# arbiter/arbiter/ingest/edgar/form13f_normalize.py
"""Store 13F holdings snapshots and compute quarter-over-quarter deltas.

A delta becomes a form13f RawFiling row (txn_type 'P' bullish / 'S' bearish).
First filing for a manager -> top-K conviction snapshot (new positions for the
K most-concentrated holdings).  Outright SHARE holdings only (puts/calls stored
but never produce deltas).  Noise floors and PIT (filing_ts = filing_date) per
the spec.  Unresolvable CUSIPs are dropped (never stored, never traded).
"""
from __future__ import annotations
import json, sqlite3
from typing import Callable
from arbiter.config import Config
from arbiter.db.helpers import generate_ulid
from arbiter.ingest.edgar.normalize import RawFiling
from arbiter.ingest.edgar.cusip_resolver import resolve_cusip

def store_holdings(conn, person_id, accession, filing_date, report_date, holdings,
                   *, asset_lookup: Callable[[], dict], now_iso: str) -> int:
    stored = 0
    for h in holdings:
        if h.get("put_call"):           # options: store for completeness, no delta
            ticker = None
        else:
            ticker = resolve_cusip(conn, h["cusip"], h.get("issuer_name", ""),
                                   asset_lookup=asset_lookup, now_iso=now_iso)
            if ticker is None:
                continue                # drop unresolved outright-share holdings
        try:
            conn.execute(
                "INSERT OR IGNORE INTO form13f_holdings "
                "(id, person_id, accession, filing_date, report_date, cusip, ticker, "
                " issuer_name, value_usd, shares, put_call, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (generate_ulid(), person_id, accession, filing_date, report_date,
                 h["cusip"], ticker, h.get("issuer_name"), float(h.get("value_usd", 0)),
                 float(h.get("shares", 0)), h.get("put_call"), now_iso),
            )
            if conn.total_changes:
                stored += 1
        except sqlite3.IntegrityError:
            pass
    conn.commit()
    return stored

def _book_total(rows: list[sqlite3.Row]) -> float:
    return sum(r["value_usd"] for r in rows) or 1.0

def _raw(person_id, ticker, filing_date, txn_type, accession, meta) -> RawFiling:
    return {
        "source": "form13f", "ticker": ticker, "person_id": person_id,
        "person_name": "",  # resolved/owned upstream; people row already exists
        "filing_ts": filing_date, "txn_type": txn_type, "txn_idx": 0,
        "shares": float(meta.get("shares", 0.0)), "price": None,
        "amount_low": None, "amount_high": None, "is_10b5_1": False,
        "is_amendment": False, "accession": accession,
        "raw_json": json.dumps(meta, default=str),
    }

def compute_deltas(conn, person_id, report_date, *, config: Config) -> list[RawFiling]:
    cur = conn.execute(
        "SELECT * FROM form13f_holdings WHERE person_id=? AND report_date=? AND put_call IS NULL",
        (person_id, report_date)).fetchall()
    if not cur:
        return []
    accession = cur[0]["accession"]; filing_date = cur[0]["filing_date"]
    book = _book_total(cur)
    # prior quarter (most recent report_date < this one) for this manager
    prior_rd = conn.execute(
        "SELECT report_date FROM form13f_holdings WHERE person_id=? AND report_date<? "
        "ORDER BY report_date DESC LIMIT 1", (person_id, report_date)).fetchone()
    min_usd = config.form13f_min_position_usd
    min_frac = config.form13f_min_book_fraction
    min_delta = config.form13f_min_delta_fraction

    def passes_floor(value_usd: float) -> bool:
        return value_usd >= min_usd and (value_usd / book) >= min_frac

    out: list[RawFiling] = []
    if prior_rd is None:
        # FIRST filing -> top-K conviction snapshot (most-concentrated, floor-passing)
        ranked = sorted([r for r in cur if passes_floor(r["value_usd"])],
                        key=lambda r: r["value_usd"], reverse=True)
        for r in ranked[: config.form13f_first_filing_top_k]:
            out.append(_raw(person_id, r["ticker"], filing_date, "P", accession,
                            {"reason": "first_filing_topk", "value_usd": r["value_usd"],
                             "book_fraction": r["value_usd"]/book, "shares": r["shares"],
                             "report_date": report_date}))
        return out

    prior = {r["ticker"]: r for r in conn.execute(
        "SELECT * FROM form13f_holdings WHERE person_id=? AND report_date=? AND put_call IS NULL",
        (person_id, prior_rd["report_date"])).fetchall()}
    now_map = {r["ticker"]: r for r in cur}
    tickers = set(now_map) | set(prior)
    for t in tickers:
        if t is None:
            continue
        p = prior.get(t); n = now_map.get(t)
        p_sh = p["shares"] if p else 0.0
        n_sh = n["shares"] if n else 0.0
        value_for_floor = (n or p)["value_usd"]
        if not passes_floor(value_for_floor):
            continue
        if p_sh == 0 and n_sh > 0:                       # new
            txn = "P"; reason = "new"
        elif p_sh > 0 and n_sh == 0:                     # exit
            txn = "S"; reason = "exit"
        elif p_sh > 0:
            change = (n_sh - p_sh) / p_sh
            if change >= min_delta:
                txn = "P"; reason = "add"
            elif change <= -min_delta:
                txn = "S"; reason = "trim"
            else:
                continue                                  # flat / tiny nibble
        else:
            continue
        out.append(_raw(person_id, t, filing_date, txn, accession,
                        {"reason": reason, "value_usd": value_for_floor,
                         "book_fraction": value_for_floor/book,
                         "shares": n_sh, "report_date": report_date}))
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/edgar/test_form13f_normalize.py -v`
Expected: PASS (all)

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && bash scripts/check_no_lookahead.sh && bash scripts/check_insert_only.sh`
Expected: both clean (`filing_ts` is passed in, never `now()`).

---

## Task 8: Detection — `_detect_fund_holdings`

**Files:**
- Modify: `arbiter/arbiter/signals/detection.py`
- Test: `tests/signals/test_detection_form13f.py`

**Interfaces:**
- Consumes: `filings` rows with `source="form13f"` (written by Task 11), `raw_json` carrying `reason`/`book_fraction`/`value_usd`.
- Produces: `Signal(signal_type=SignalType.FUND_HOLDING, source="form13f", ...)` one per row; conviction from event-cleanliness × book_fraction, **capped at `config`-equivalent 0.7** (use a module constant `_FUND_MAX_CONVICTION = 0.7`); `meta["txn_type"]` carried for the sign flip.

- [ ] **Step 1: Write the failing test**

```python
# tests/signals/test_detection_form13f.py
import json, sqlite3
from datetime import datetime, timezone
from arbiter.db.migrate import apply_migrations
from arbiter.db.helpers import generate_ulid
from arbiter.signals.detection import detect_signals, SignalType

NOW = datetime(2026, 6, 23, tzinfo=timezone.utc)

def _conn():
    c = sqlite3.connect(":memory:"); c.row_factory = sqlite3.Row; apply_migrations(c); return c

def _filing(c, ticker, txn, book_frac, reason="new"):
    c.execute(
        "INSERT INTO filings (id, source, ticker, person_id, filing_ts, txn_type, "
        "txn_idx, shares, is_10b5_1, is_amendment, is_superseded, accession, raw_json) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (generate_ulid(), "form13f", ticker, "p1", "2026-05-15T00:00:00+00:00", txn, 0,
         1000, 0, 0, 0, "acc1",
         json.dumps({"reason": reason, "book_fraction": book_frac, "value_usd": 60e6})))
    c.commit()

def test_fund_signal_conviction_capped_and_sign_meta():
    c = _conn()
    _filing(c, "NVDA", "P", 0.5, "new")   # very concentrated new buy
    _filing(c, "TSLA", "S", 0.3, "exit")
    sigs = [s for s in detect_signals(c, NOW) if s.source == "form13f"]
    assert {s.ticker for s in sigs} == {"NVDA", "TSLA"}
    nv = next(s for s in sigs if s.ticker == "NVDA")
    assert nv.signal_type == SignalType.FUND_HOLDING
    assert nv.conviction_score <= 0.7            # hard cap
    assert nv.meta["txn_type"] == "P"
    ts = next(s for s in sigs if s.ticker == "TSLA")
    assert ts.meta["txn_type"] == "S"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/signals/test_detection_form13f.py -v`
Expected: FAIL — `AttributeError: FUND_HOLDING` / no form13f signals.

- [ ] **Step 3: Implement**

In `signals/detection.py`: add `FUND_HOLDING = "fund_holding"` to `SignalType`. Add a `form13f` SELECT (mirror the `sc13_sql` block, `txn_type IN ('P','S')`, select `raw_json`) and a `_detect_fund_holdings(rows, *, as_of)` sub-detector appended to `detect_signals`:

```python
_FUND_MAX_CONVICTION = 0.7

def _detect_fund_holdings(rows: list, *, as_of: datetime) -> list[Signal]:
    """One signal per 13F delta row. Conviction = event-cleanliness x concentration,
    hard-capped (13F is stale). txn_type carried in meta for the stance sign."""
    results: list[Signal] = []
    for row in rows:
        ts = _parse_ts(row["filing_ts"])
        if ts > as_of:
            continue  # no look-ahead
        meta = json.loads(row["raw_json"]) if row["raw_json"] else {}
        reason = meta.get("reason", "add")
        book_frac = float(meta.get("book_fraction") or 0.0)
        # clean new/exit are the strongest; add/trim a notch lower
        base = 0.45 if reason in ("new", "exit", "first_filing_topk") else 0.30
        boost = min(book_frac / 0.10, 1.0) * 0.25      # 10%+ of book => full boost
        conviction = round(min(base + boost, _FUND_MAX_CONVICTION), 4)
        results.append(Signal(
            signal_type=SignalType.FUND_HOLDING, ticker=row["ticker"], source="form13f",
            person_ids=(row["person_id"],), filing_ids=(row["id"],),
            window_start=ts, window_end=ts, conviction_score=conviction,
            meta={"txn_type": row["txn_type"], "reason": reason, "book_fraction": book_frac},
            as_of=as_of))
    return results
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/signals/test_detection_form13f.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && .venv/bin/python -m pytest tests/signals/ -q && bash scripts/check_no_lookahead.sh`
Expected: PASS + clean.

---

## Task 9: Emit — `A1.fund` mapping + bearish sign flip

**Files:**
- Modify: `arbiter/arbiter/signals/emit.py`
- Test: `tests/signals/test_emit_form13f.py`

**Interfaces:**
- Consumes: `Signal(source="form13f", meta={"txn_type": ...})`.
- Produces: `Opinion(advisor_id="A1.fund", horizon_days=180, stance_score sign-flipped on 'S')`.

- [ ] **Step 1: Write the failing test**

```python
# tests/signals/test_emit_form13f.py
from datetime import datetime, timezone
from arbiter.signals.detection import Signal, SignalType
from arbiter.signals.emit import emit_opinion

NOW = datetime(2026, 6, 23, tzinfo=timezone.utc)

def _sig(ticker, txn, conv=0.6):
    return Signal(signal_type=SignalType.FUND_HOLDING, ticker=ticker, source="form13f",
                  person_ids=("p1",), filing_ids=("f1",), window_start=NOW, window_end=NOW,
                  conviction_score=conv, meta={"txn_type": txn}, as_of=NOW)

def test_emit_fund_long():
    op = emit_opinion(_sig("NVDA", "P"), NOW)
    assert op is not None
    assert op.advisor_id == "A1.fund"
    assert op.horizon_days == 180
    assert op.stance_score > 0

def test_emit_fund_exit_is_bearish():
    op = emit_opinion(_sig("TSLA", "S"), NOW)
    assert op is not None and op.advisor_id == "A1.fund"
    assert op.stance_score < 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/signals/test_emit_form13f.py -v`
Expected: FAIL — advisor_id is the fallback `A1.insider`, sign not flipped.

- [ ] **Step 3: Implement**

In `emit.py`: add `_ADVISOR_ID_FUND = "A1.fund"` and `_HORIZON_DAYS_FUND = 180`. Extend the source→advisor mapping:

```python
elif signal.source == "form13f":
    advisor_id = _ADVISOR_ID_FUND
    horizon_days = _HORIZON_DAYS_FUND
```

Extend the bearish sign-flip to cover form13f exits/trims:

```python
if signal.meta.get("txn_type") == "S" and signal.source in ("form13d", "form13f"):
    stance_score = -stance_score
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/signals/test_emit_form13f.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && .venv/bin/python -m pytest tests/signals/ -q`
Expected: PASS

---

## Task 10: Advisor fn + engine wiring (`A1.fund` in the council)

**Files:**
- Modify: `arbiter/arbiter/engine/advisors.py`
- Modify: `arbiter/arbiter/engine/__init__.py`
- Modify: `arbiter/arbiter/engine/_engine.py` (advisor_map registration + horizon set ~line 527)
- Test: `tests/engine/test_a1_fund_advisor.py`

**Interfaces:**
- Consumes: `detect_signals`, `emit_opinion`, `score_signal` (as the activist fn does).
- Produces: `_build_a1_fund_fn(db_path, pit, clock) -> Callable[[], Opinion | None]`; `advisor_map` gains key `"A1.fund"`; horizon set includes `"form13f"` → 180.

- [ ] **Step 1: Write the failing test**

```python
# tests/engine/test_a1_fund_advisor.py
import json, sqlite3
from datetime import datetime, timezone
from arbiter.db.migrate import apply_migrations
from arbiter.db.helpers import generate_ulid

NOW = datetime(2026, 6, 23, tzinfo=timezone.utc)

def _seed_form13f_filing(path):
    c = sqlite3.connect(path); c.row_factory = sqlite3.Row; apply_migrations(c)
    c.execute("INSERT INTO filings (id, source, ticker, person_id, filing_ts, txn_type, "
              "txn_idx, shares, is_10b5_1, is_amendment, is_superseded, accession, raw_json) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (generate_ulid(), "form13f", "NVDA", "p1", "2026-05-15T00:00:00+00:00", "P",
               0, 1000, 0, 0, 0, "acc1",
               json.dumps({"reason": "new", "book_fraction": 0.5, "value_usd": 60e6})))
    c.commit(); c.close()

def test_a1_fund_fn_emits_opinion(tmp_path):
    from arbiter.engine.advisors import _build_a1_fund_fn
    from arbiter.types import FixedClock  # or the repo's test clock
    path = str(tmp_path / "t.db"); _seed_form13f_filing(path)
    fn = _build_a1_fund_fn(path, pit=None, clock=FixedClock(NOW))
    op = fn()
    assert op is not None and op.advisor_id == "A1.fund" and op.ticker == "NVDA"
```

(Confirm the project's test clock name — grep tests for `Clock` fakes; reuse the established one.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/engine/test_a1_fund_advisor.py -v`
Expected: FAIL — `ImportError: _build_a1_fund_fn`.

- [ ] **Step 3: Implement the advisor fn (mirror `_build_a1_activist_fn` exactly)**

```python
def _build_a1_fund_fn(db_path, pit, clock):
    """Zero-arg callable producing Opinion | None for A1.fund (13F deltas)."""
    def _fn():
        as_of = clock.now()
        thread_conn = get_connection(db_path)
        try:
            signals = detect_signals(thread_conn, as_of, cluster_min_people=2)
            fund = [s for s in signals if s.source == "form13f"]
            if not fund:
                return None
            best = max(fund, key=lambda s: s.conviction_score)
            score_bundle = score_signal(best, as_of)
            return emit_opinion(best, as_of, score_bundle)
        finally:
            thread_conn.close()
    return _fn
```

Export it in `engine/__init__.py` (add to the import block and `__all__`). In `engine/_engine.py`:
- add `"form13f"` to the 180-day horizon set at ~line 527: `horizon = 180 if sig.source in ("form4", "form13d", "form13f") else 90`.
- register the advisor in the `advisor_map` construction (find where `_build_a1_activist_fn` is added — usually near line ~1014 — and add `"A1.fund": _build_a1_fund_fn(db_path, pit, clock)` alongside it).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/engine/test_a1_fund_advisor.py -v`
Expected: PASS

- [ ] **Step 5: Add the orphan-attribution regression test**

This is the A3 bug class — prove the A1.fund opinion links to its spawned idea (matched 180d/LONG bucket). Add to the same test file an engine-level test that runs a cycle on a `form13f`-seeded DB (sim backend) and asserts a persisted opinion has `idea_id` set and equal to the spawned idea's id. Mirror the existing A3 wiring test (`grep -rn "op.idea_id == idea" tests/`), swapping the seed to a `form13f` filing.

- [ ] **Step 6: Checkpoint**

Run: `cd arbiter && KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/engine/ -q`
Expected: PASS

---

## Task 11: Runner ingest + people registration + source wiring

**Files:**
- Modify: `arbiter/arbiter/ingest/runner.py`
- Test: `tests/ingest/test_runner_form13f.py`

**Interfaces:**
- Consumes: `FUND_MANAGERS` (Task 3), `EdgarClient.search_form13f_filings`/`get_form13f_info_table` (Task 6), `parse_form13f_infotable` (Task 5), `store_holdings`/`compute_deltas` (Task 7), `write_filing` (existing), `resolve_person` (existing).
- Produces: `_ingest_form13f(config, *, conn, clock, summary)` registered when `"form13f"` ∈ sources; managers upserted into `people` (`source="form13f"`); default `sources` extended to include `"form13f"`.

- [ ] **Step 1: Write the failing test** (inject a fake EdgarClient + fake Alpaca asset lookup; no network)

```python
# tests/ingest/test_runner_form13f.py (append)
import sqlite3
from datetime import datetime, timezone
from arbiter.db.migrate import apply_migrations
from arbiter.config import Config

NOW = datetime(2026, 6, 23, tzinfo=timezone.utc)

class _FakeEdgar:
    def search_form13f_filings(self, cik, *, count=8):
        return [{"cik": cik, "accession": "acc1", "filed_at": "2026-05-15",
                 "report_date": "2026-03-31", "primary_document": "primary_doc.xml",
                 "is_amendment": False}]
    def get_form13f_info_table(self, accession, cik):
        return ("<informationTable xmlns='http://www.sec.gov/edgar/document/thirteenf/informationtable'>"
                "<infoTable><nameOfIssuer>NVIDIA CORP</nameOfIssuer><cusip>67066G104</cusip>"
                "<value>60000000</value><shrsOrPrnAmt><sshPrnamt>1000</sshPrnamt></shrsOrPrnAmt>"
                "</infoTable></informationTable>")

def test_ingest_form13f_writes_people_holdings_and_filing(monkeypatch, tmp_path):
    from arbiter.ingest import runner
    monkeypatch.setattr(runner, "_make_edgar_for_form13f", lambda cfg: _FakeEdgar())  # see Step 3
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {"NVIDIA CORP": "NVDA"}))
    conn = sqlite3.connect(str(tmp_path/"t.db")); conn.row_factory = sqlite3.Row; apply_migrations(conn)
    from arbiter.types import FixedClock
    summary = runner.IngestSummary(sources=("form13f",))
    runner._ingest_form13f(Config.load(), conn=conn, clock=FixedClock(NOW), summary=summary)
    # manager registered as a person
    assert conn.execute("SELECT COUNT(*) c FROM people WHERE source='form13f'").fetchone()["c"] >= 11
    # holdings stored
    assert conn.execute("SELECT COUNT(*) c FROM form13f_holdings").fetchone()["c"] >= 1
    # first-filing top-K delta written to filings as form13f
    assert conn.execute("SELECT COUNT(*) c FROM filings WHERE source='form13f'").fetchone()["c"] >= 1

def test_ingest_form13f_inert_under_backtest():
    from arbiter.ingest import runner
    from arbiter.types import BacktestClock  # confirm exact import
    # Under a BacktestClock the function must early-return without network.
    # (Implementation guards on isinstance(clock, BacktestClock).)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/test_runner_form13f.py -v -k ingest`
Expected: FAIL — `_ingest_form13f` not defined.

- [ ] **Step 3: Implement `_ingest_form13f` (mirror `_ingest_sc13`'s structure + guards)**

Key points (read `_ingest_sc13` first and follow it):
- Guard: empty `config.edgar_user_agent` → log warning + return (inert, not a crash).
- Guard: `isinstance(clock, BacktestClock)` → return immediately (no network, no lookahead).
- Register managers in `people`: for each `FundManager`, `resolve_person(m.name, source="form13f", conn=conn, hints={...})` (or the existing upsert path the other sources use) so each gets a stable `person_id`.
- For each manager CIK: `client.search_form13f_filings(cik)`; for the newest 1–2 filings, `xml = client.get_form13f_info_table(acc, cik)`; `holdings = parse_form13f_infotable(xml)`; `store_holdings(conn, person_id, acc, filing_date, report_date, holdings, asset_lookup=..., now_iso=clock.now().isoformat())`; `deltas = compute_deltas(conn, person_id, report_date, config=config)`; set each delta's `person_id` to the resolved person and `write_filing(conn, delta, ...)`.
- `asset_lookup`: a thin `_alpaca_asset_lookup(config)` returning a cached `{issuer_name_upper: ticker}` from the existing AlpacaAdapter `/v2/assets` (class=us_equity, active, tradable). Factor as a helper so the test can monkeypatch it.
- Add `"form13f"` to the default `sources` tuple in `run_ingest` and the dispatch (`if "form13f" in sources_tuple: _ingest_form13f(...)`).
- `summary.per_source["form13f"]` accounting like the others.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd arbiter && .venv/bin/python -m pytest tests/ingest/test_runner_form13f.py -v`
Expected: PASS

- [ ] **Step 5: Checkpoint**

Run: `cd arbiter && KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ingest/ -q && bash scripts/check_no_lookahead.sh && bash scripts/check_insert_only.sh`
Expected: PASS + both clean.

---

## Task 12: Cockpit `A1.fund` advisor node

**Files:**
- Modify: `cockpit/api/graph.py` (or wherever advisor nodes are declared — grep `A3.news`/`A1.activist`)
- Test: `cockpit/api/` pytest + `cockpit/web` tsc

**Interfaces:**
- Produces: the `/graph` payload includes an `A1.fund` advisor node in the council cluster (un-dimmed, `future=False`).

- [ ] **Step 1: Write/extend the failing test**

```python
# cockpit/api/test_api.py (append)
def test_graph_includes_a1_fund_node(client):
    nodes = client.get("/graph").json()["nodes"]
    ids = {n["id"] for n in nodes}
    assert "A1.fund" in ids
    fund = next(n for n in nodes if n["id"] == "A1.fund")
    assert fund.get("future") in (False, None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd arbiter && .venv/bin/python -m pytest ../cockpit/api/test_api.py::test_graph_includes_a1_fund_node -v`
Expected: FAIL — no such node.

- [ ] **Step 3: Add the node**

Find where `A1.activist`/`A3.news` advisor nodes are defined in the cockpit graph builder and add an `A1.fund` node ("Fund Managers (13F)") to the council/advisor cluster, mirroring the `A3.news` entry that was un-dimmed.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd arbiter && .venv/bin/python -m pytest ../cockpit/api -q`
Then: `cd cockpit/web && npx tsc --noEmit && npx vitest run`
Expected: PASS / clean.

- [ ] **Step 5: Checkpoint**

Run: cockpit api + web suites green (above).

---

## Task 13: Full-suite gate + live supervised smoke + deploy

**Files:** none (verification + deploy).

- [ ] **Step 1: Full hermetic suite + linters**

Run:
```bash
cd arbiter && KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ -q
bash scripts/check_no_lookahead.sh && bash scripts/check_insert_only.sh
```
Expected: all green (baseline ~2441 + new tests), both linters clean.

- [ ] **Step 2: Cockpit suites**

Run: `cd cockpit/web && npx tsc --noEmit && npx vitest run` and `cd arbiter && .venv/bin/python -m pytest ../cockpit/api -q`
Expected: green.

- [ ] **Step 3: Live read-only smoke (supervised, market hours not required)**

Run a real one-shot 13F ingest and inspect what it writes (does NOT place orders):
```bash
cd arbiter && .venv/bin/python -m arbiter ingest --sources form13f
.venv/bin/python -c "
import sqlite3; c=sqlite3.connect('file:data/arbiter.db?mode=ro',uri=True); c.row_factory=sqlite3.Row
print('people(form13f):', c.execute(\"SELECT COUNT(*) n FROM people WHERE source='form13f'\").fetchone()['n'])
print('holdings:', c.execute('SELECT COUNT(*) n FROM form13f_holdings').fetchone()['n'])
print('form13f filings:', c.execute(\"SELECT COUNT(*) n FROM filings WHERE source='form13f'\").fetchone()['n'])
for r in c.execute(\"SELECT ticker,txn_type,raw_json FROM filings WHERE source='form13f' LIMIT 8\"):
    print(' ', r['ticker'], r['txn_type'])
"
```
Expected: managers registered, holdings stored, a sane number of `form13f` filings (a few per manager, not hundreds). Eyeball that tickers are real/tradeable and unresolved CUSIPs were dropped (check logs for `cusip.unresolved`).

- [ ] **Step 4: Confirm no live order placed; restart the daemon to load the new advisor**

The next live `run_cycle` will fuse `A1.fund` probationally. Restart the daemon so the code loads:
```bash
launchctl kickstart -k "gui/$(id -u)/com.arbiter.daemon"
```
Watch the next market-open cycle (the user supervises). The learning loop governs A1.fund weight from here.

- [ ] **Step 5: Update memory**

Append the outcome to the `arbiter-project` memory tail (new advisor `A1.fund`, the manager roster, the CUSIP-resolution scope cut, test count) and add a one-line MEMORY.md note if warranted.

---

## Self-Review

**Spec coverage:** §1 purpose → roster (T3) + advisor (T8–T11); §2 hard-truth → 180d/cap 0.7 (T8/T9), delta-only (T7), PIT filing_date (T7), longs-only (T7 put_call skip); §3 decisions table → all mapped (delta T7, expand-universe = filings-driven idea spawning, top-5 T7, noise floors T7, stance T8/T9, cadence T11, probationary weight = EQUAL_FLOOR inherited); §5 roster → T3; §6 CUSIP → T4 + sector note (verify in T11/T13); §7 stance math → T8/T9; §8 config → T2; §9 cockpit → T12; §10 testing → every task; §11 migrations → T1; §12 risks → honored (drop unresolved, deltas-only). **Gap check:** sector/SIC handling for new tickers (§6) is asserted-by-reuse, not a dedicated task — verified live in T13 Step 3 (eyeball) since Congress/Form-4 already exercise the off-watchlist sector path; if T13 surfaces an `UNKNOWN`-sector failure, add a follow-up task.

**Placeholder scan:** the only intentional "fill-in" is the roster CIKs (T3) — these REQUIRE live EDGAR verification and cannot be fabricated; T3 Steps 3+5 make that an explicit, gated action with a verification command. No other TBDs.

**Type consistency:** `parse_form13f_infotable` dict keys (`cusip/issuer_name/value_usd/shares/put_call`) are consumed identically by `store_holdings` (T7). `resolve_cusip(conn, cusip, issuer_name, *, asset_lookup, now_iso)` signature matches its callers (T7). `compute_deltas(conn, person_id, report_date, *, config)` and `store_holdings(...)` names match T11 calls. `_build_a1_fund_fn(db_path, pit, clock)` matches `_build_a1_activist_fn`. `SignalType.FUND_HOLDING`, `advisor_id="A1.fund"`, `source="form13f"`, horizon 180 are used consistently across T8/T9/T10. `RawFiling` keys match `writer.write_filing`'s schema.
