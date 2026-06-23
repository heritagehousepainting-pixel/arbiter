# EDGAR Insider Filings — Design Spec

**Date:** 2026-06-19
**Owner domain (strict):** `arbiter/ingest/edgar/**` + `tests/ingest/edgar/**`
**Scope:** (1) Fix broken Form-4 discovery (#1). (2) Add a 13D/13G activist-stake advisor (#4b).
**Planning agent only — no code is written by this document.** Anything that needs `engine.py` is listed under **FOR WAVE 2 (engine owner)**.

---

## 0. Orientation — how the pipeline actually flows today

The EDGAR ingest is a **data-producing** lane, not an opinion-producing lane. Confirmed by reading the code:

```
EdgarClient.search_form4_filings(ticker)   # discovery: ticker -> [{cik, accession, filed_at}]
   -> EdgarClient.get_form4_xml(acc, cik)  # fetch: -> raw XML string
   -> parse_form4(xml, ticker, accession)  # parse: -> list[dict] (per-transaction rows)
   -> normalize(parsed_rows)               # business rules: -> list[RawFiling]
   -> writer.write_filing(conn, raw, clock)  # persist into the `filings` table
```

Downstream, **the advisor reads the `filings` table, not the ingest lane**:

- `arbiter/signals/detection.py::detect_signals` selects `filings` rows by `source = 'form4'` / `source = 'congress'` (detection.py:187, :197) and produces `Signal` objects carrying a `source` field (detection.py:90).
- `arbiter/signals/emit.py::emit_opinion` maps `Signal.source -> advisor_id + horizon` (emit.py:108-115) and returns an `Opinion`.
- `arbiter/engine.py::_build_a1_insider_fn` / `_build_a1_congress_fn` (engine.py:135-192) wrap `detect_signals -> score_signal -> emit_opinion`, filtering on `s.source == "form4"` / `"congress"`.

**Key consequence for this spec:** Form-4 and 13D/13G both plug into the engine **through the `filings` table** keyed by the `source` string — *not* by exporting a `build_*_opinion()` function from the ingest package. The "wiring contract" the engine owner consumes is therefore:
1. the **`source` string** written into `filings`, and
2. the **ingest entry-point** the runner calls to populate those rows.

The `filings.source` column is plain `TEXT NOT NULL` with **no CHECK constraint** (`arbiter/db/migrations/001_core.sql:43`), so a new source value (`"form13d"`) is schema-compatible with **no migration required**.

---

## 1. CURRENT STATE

### 1.1 The EDGAR fetch path (today)

`EdgarClient` (`arbiter/ingest/edgar/client.py`) is the only network surface.

- **Construction** requires `Config.edgar_user_agent`; empty -> `ValueError` (client.py:66-70). The runner already guards this and skips form4 gracefully (runner.py:197-205), so an unset `EDGAR_USER_AGENT` does **not** crash the run today — but it also means form4 is *entirely inert* whenever the var is unset (which is the current state, per project memory).
- **`get_form4_xml(accession, cik)`** (client.py:83-118): builds `…/Archives/edgar/data/{cik}/{accession_nodashes}/{accession}-index.htm`, GETs it, then `_extract_xml_filename` (client.py:186-202) regexes the first `*.xml` href and fetches it. This path is plausible and **not the break**.
- **`search_form4_filings(ticker, count)`** (client.py:120-137): the **discovery** step. Builds a legacy `browse-edgar` URL with `output=atom`, GETs it, and hands the body to `_parse_edgar_atom`.

### 1.2 Where discovery breaks (root cause — cite)

**`_parse_edgar_atom` (client.py:205-234) cannot extract `accession` or `cik` from a `browse-edgar` company-feed, so every discovered filing has empty `accession`/`cik`, and the runner drops 100% of them.**

Precise failure chain:

1. client.py:130-134 requests `…/cgi-bin/browse-edgar?action=getcompany&company={ticker}&type=4&…&output=atom`. The `&company={ticker}` parameter is a **company-name** search, not a ticker lookup; the correct modern EDGAR discovery is the company **submissions JSON** (`https://data.sec.gov/submissions/CIK##########.json`) keyed by CIK, or the full-text search API. The browse-edgar atom branch is legacy and unreliable.
2. Even when the feed returns entries, client.py:224-225 looks for elements in the namespace `{http://www.sec.gov/Archives/edgar}accession-number` and `…cik`. **The browse-edgar company atom feed does not nest `<accession-number>`/`<cik>` under that namespace** — the accession lives inside the `<id>`/`<link href>` of each entry (e.g. `…-index.htm`), and the issuer CIK is in a single feed-level `<company-info>` block, not per-entry. So `acc_el`/`cik_el` resolve to `None` and client.py:229-230 emit `accession=""`, `cik=""`.
3. Back in the runner, `_ingest_form4_ticker` (runner.py:251-255): `if not accession or not cik: src.n_skipped += 1; continue`. **Every** discovered filing is skipped. Form-4 ingest therefore writes **zero rows every run** even when `EDGAR_USER_AGENT` is set — discovery is structurally broken, independent of the env var.

**Summary of the two distinct problems:**
- **P-A (env):** when `EDGAR_USER_AGENT` is unset, form4 is skipped (already graceful — runner.py:197). Must be *preserved*, and a clear single log line kept.
- **P-B (bug):** when `EDGAR_USER_AGENT` *is* set, discovery still yields empty `accession`/`cik` and writes nothing. This is the real defect.

---

## 2. FORM-4 FIX DESIGN (within `arbiter/ingest/edgar/**` only)

### 2.1 Root cause restated

Discovery uses the wrong endpoint + a parser that targets a non-existent feed shape. Fix = replace the discovery transport + parser with the **EDGAR submissions JSON** path, which is the SEC-blessed, stable, machine-readable index and is keyed by CIK (which is exactly what `get_form4_xml` already needs).

### 2.2 File-by-file changes

#### `arbiter/ingest/edgar/client.py`

**(a) Add ticker -> CIK resolution.** EDGAR publishes `https://www.sec.gov/files/company_tickers.json` (a static map of `{ticker -> cik}`). Add:
- New method `get_cik_for_ticker(ticker: str) -> str | None`. Fetches/caches `company_tickers.json` once per client instance (store the parsed map on `self._ticker_cik_map`, lazily). Returns the 10-digit zero-padded CIK or `None`. Uses the existing `self._get` (rate-limit + retry already correct).
- A module-level constant `_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"`.

**(b) Replace `search_form4_filings` discovery transport.** Reimplement it to:
1. Resolve CIK via `get_cik_for_ticker(ticker)`. If `None` -> return `[]` (log a `debug` "no CIK for ticker").
2. GET `https://data.sec.gov/submissions/CIK{cik10}.json` (new constant `_SUBMISSIONS_URL_TMPL`).
3. Parse via new `_parse_submissions_json(body, cik, *, form_type="4", count=20)` -> `list[dict]` of `{cik, accession, filed_at, primary_document}`.
   - The submissions JSON has a `filings.recent` object with **parallel arrays**: `form`, `accessionNumber`, `filingDate`, `primaryDocument`. Zip them, keep rows where `form == form_type` (i.e. `"4"`), newest-first, take `count`.
   - `accessionNumber` arrives dash-formatted (`"0001234567-26-000001"`) — exactly what `get_form4_xml` expects. `cik` is the issuer CIK we resolved.
   - **Capture `primaryDocument`** so `get_form4_xml` can skip the index-page round-trip entirely (see (c)). This both fixes the break and halves the request count.

**(c) Make `get_form4_xml` accept an optional known primary document.** Change signature to `get_form4_xml(accession, cik, *, primary_document: str | None = None)`. When `primary_document` is provided and ends in `.xml`, build the document URL directly and skip the index fetch + `_extract_xml_filename` regex. When `None`, fall back to the existing index-scrape path (kept for safety). This keeps `_extract_xml_filename` (client.py:186) as the fallback only.

**(d) Delete or quarantine `_parse_edgar_atom`** (client.py:205-234) — it is the broken parser. Remove the `output=atom` branch entirely. (No external caller imports it; only `search_form4_filings` used it.)

**(e) Graceful-skip on unset `EDGAR_USER_AGENT` (within edgar/**).** The runner already guards (runner.py:197), but harden the **client** so the library is safe in isolation:
- Add a classmethod/factory `EdgarClient.from_config_or_none(config) -> EdgarClient | None` that returns `None` (instead of raising) when `config.edgar_user_agent` is empty/whitespace, logging exactly one line: `log.warning("edgar.disabled_no_user_agent", reason="EDGAR_USER_AGENT unset; EDGAR ingest (Form-4 + 13D/G) inert")`.
- Keep the existing `__init__` `ValueError` for callers that construct directly (back-compat; the runner constructs in a `try/except` already).
- **FOR WAVE 2 / runner note:** the runner may optionally switch to `from_config_or_none`, but is NOT required to — its existing guard already satisfies "inert, not crash". This is listed under §6 as optional.

#### `arbiter/ingest/edgar/parser.py`

No change required for the Form-4 fix. (`parse_form4` already correctly handles the XML once it is fetched.) Confirmed by the existing passing tests in `tests/ingest/test_edgar.py`.

#### `arbiter/ingest/edgar/normalize.py`

No change required for the Form-4 fix.

#### `arbiter/ingest/edgar/__init__.py`

Export the new public surface so the runner can import cleanly:
- Add `get_cik_for_ticker` is a method (no new top-level export needed).
- No new top-level symbols required for the Form-4 fix; `EdgarClient`, `parse_form4`, `normalize`, `RawFiling` stay as-is.

### 2.3 Behavior after the fix

- `EDGAR_USER_AGENT` unset -> form4 (and 13D/G) inert, one WARNING line, Congress still runs. (unchanged-but-preserved)
- `EDGAR_USER_AGENT` set -> `search_form4_filings` returns real `{cik, accession, filed_at, primary_document}` rows from the submissions JSON; `get_form4_xml` fetches the primary doc directly; rows flow through `parse_form4 -> normalize -> write_filing` and **actually persist**.

---

## 3. 13D/13G ACTIVIST-STAKE DESIGN (#4b)

### 3.1 What a 13D/13G is and why it is a signal

A **Schedule 13D** is filed by anyone acquiring **>5% beneficial ownership** of a public company with **intent to influence/control** (activist). A **Schedule 13G** is the passive-investor short-form variant (>5%, no control intent). 13D especially is a strong, well-documented **bullish activist** signal: the filer is taking a large concentrated stake and frequently agitating for value-unlocking change. We model 13D as a higher-conviction long signal than 13G.

### 3.2 New source string and DB strategy

- New `filings.source = "form13d"` (covers both 13D and 13G; the schedule variant is recorded in `raw_json` and in `txn_type`). No migration — `source` is unconstrained TEXT.
- These rows ride the **same `filings` table and the same `RawFiling` shape** as Form-4 (per directive: "the same internal opinion-producing shape"). `txn_type = "P"` is used so the existing detection `txn_type = 'P'` purchase filter naturally includes a new branch the engine owner adds (see §6). We set `txn_type = "P"` for **acquisitions of a new/increased stake** and `txn_type = "S"` for an exit/reduction (a 13D/A reporting the stake dropped below the threshold).

### 3.3 New files under `arbiter/ingest/edgar/`

#### `arbiter/ingest/edgar/sc13_parser.py` (new)

`parse_sc13(raw_text: str, ticker: str, accession: str, *, schedule: str) -> list[dict]`

13D/13G come in two on-the-wire forms; support both, preferring the structured one:
- **Structured XML** (`<edgarSubmission>` / `ownershipDocument`-style for newer filings) — primary path: extract issuer CIK/name, **subject company** (the target — this is the ticker we trade), reporting/filing person (the activist), `cusip`, **percent of class** (`<percentOfClass>`), aggregate amount beneficially owned (`<aggregateAmountOwned>`), event date / date of event requiring filing, and `<documentType>` (`SC 13D`, `SC 13D/A`, `SC 13G`, `SC 13G/A`).
- **Header-only fallback** (older plain-text `.txt` with a `<SEC-HEADER>` block): regex the `SUBJECT COMPANY`, `FILED BY`, `CONFORMED SUBMISSION TYPE`, `FILED AS OF DATE`, and `CUSIP`. Percent-of-class is often only in the body free-text; attempt a tolerant regex (`r"([0-9]{1,3}(?:\.[0-9]+)?)\s*%"` near "percent of class"), else leave `None`.

Output row dict (mirrors the Form-4 parser's row contract so `normalize` is trivial):
```python
{
    "ticker": str,                  # subject-company ticker
    "person_id": str,               # filer/activist CIK (the reporting owner)
    "person_name": str,             # filer name
    "filing_ts": str,               # tz-aware ISO-8601 UTC (event date, fallback filing date)
    "schedule": "13D" | "13G",      # parsed schedule
    "is_amendment": bool,           # "/A" in documentType
    "is_activist": bool,            # True for 13D, False for 13G
    "percent_of_class": float | None,
    "aggregate_amount": float | None,   # shares beneficially owned
    "cusip": str | None,
    "transaction_code": "P" | "S",  # P=new/increased stake, S=exit/reduction
    "txn_idx": 0,                   # one row per filing
    "accession": str,
    "is_10b5_1": False,             # not applicable; always False (defense-in-depth shape parity)
}
```
Reuse `parser.py::_parse_filing_ts`, `_text`, `_float`, `_float_or_none` (either import them or factor them into a tiny shared `_xmlutil` helper — **own-domain only**; preference: import from `parser.py` to avoid a new module).

#### `arbiter/ingest/edgar/sc13_normalize.py` (new)

`normalize_sc13(parsed: list[dict]) -> list[RawFiling]`

Business rules:
1. Drop rows with `percent_of_class is not None and percent_of_class < 5.0` (defensive; SEC threshold is 5% — anything below is a data error or an exit-below-threshold which we still keep as an `S`).
2. `txn_type = row["transaction_code"]` (`"P"` or `"S"`).
3. Map into the existing `RawFiling` TypedDict (reused from `normalize.py`) with:
   - `source = "form13d"`
   - `shares = aggregate_amount or 0.0`
   - `amount_low / amount_high = None` (dollar value not disclosed on the schedule; keep `None` — never fabricate, consistent with the Form-4 `None`-preservation rule).
   - `is_10b5_1 = False`, `is_amendment` forwarded.
   - `raw_json = json.dumps(row)` — preserves `schedule`, `percent_of_class`, `is_activist`, `cusip` for the engine owner's detector/scorer to read.
- **Amendment handling:** a `13D/A` or `13G/A` sets `is_amendment=True`; the existing `writer.write_filing` supersede logic (writer.py:217-250) then flips prior rows for the same `(ticker, person_id)`. This is exactly the desired behavior: an updated stake supersedes the prior stake row.

#### `arbiter/ingest/edgar/client.py` (extend)

Add discovery + fetch for 13D/13G using the **same submissions-JSON transport** built in §2.2:
- `search_sc13_filings(ticker, *, count=20) -> list[dict]`: same as `search_form4_filings` but filter `filings.recent.form` for `{"SC 13D", "SC 13D/A", "SC 13G", "SC 13G/A"}`. Each result dict adds `"schedule"` (`"13D"`/`"13G"`) and `"is_amendment"`.
  - **Note on subject vs filer:** the submissions JSON of the *subject company* (the ticker we resolved) lists 13D/G filings made **against** it. That is exactly what we want — we resolve the ticker's CIK and read who filed a 13D/G on it. The activist's CIK/name comes from parsing the document body.
- `get_sc13_doc(accession, cik, *, primary_document=None) -> str`: identical pattern to `get_form4_xml` (reuse the same index/primary-doc helper; consider extracting a private `_fetch_primary_doc(accession, cik, primary_document, *, suffixes=(".xml",".txt"))` used by both — own-domain refactor, optional).

#### `arbiter/ingest/edgar/__init__.py` (extend)

Add exports: `parse_sc13`, `normalize_sc13`, and the new client methods are reached via `EdgarClient`. Update the module docstring to mention the 13D/G surface.

### 3.4 Activist-stake -> opinion mapping (semantics the engine owner will implement; defined here, frozen)

The ingest lane does **not** emit opinions — it writes `filings` rows. But this spec **defines the mapping** so the Wave-2 engine owner can wire it mechanically. Recorded here and in §6.

| Aspect | 13D (activist) | 13G (passive) |
|---|---|---|
| `advisor_id` | `A1.activist` | `A1.activist` (same advisor; 13G is the weaker member) |
| stance sign | **+** (long) for `P`; **−** for `S` (exit) | same |
| base conviction | **0.70** | **0.35** |
| `percent_of_class` boost | `+min(percent_of_class/50.0, 0.30)` (a 15% stake adds ~0.09; capped) | same formula |
| `confidence` | conviction, clamped to (0.01, 1.0] | same |
| `confidence_source` | `ConfidenceSource.MODELED` | `MODELED` |
| `horizon_days` | **180** (LONG bucket; activist theses play out over months) | **180** |
| abstain | `percent_of_class < 5%` and no other signal, or amendment that only reduces below threshold with no `S` interest | same |

Rationale for the numbers: 13D filers are concentrated, intentional, and historically alpha-generative; we seed conviction **above** a single-insider Form-4 buy (which starts near 0.0 and scales by dollar size) but **below** a strong multi-insider cluster. 13G is passive money — informative but weaker, so half the base. Horizon is LONG because activist campaigns and large-stake repricings unfold over 1–6 months. These are **cold-start priors**; the learning loop (#4) will recalibrate `A1.activist` from real outcomes exactly as it does for `A1.insider`/`A1.congress`.

### 3.5 New advisor id (frozen)

**`A1.activist`** — registered by the engine owner, consistent with the `A1.*` smart-money family (`A1.insider`, `A1.congress`).

---

## 4. TEST PLAN (offline only; `tests/ingest/edgar/`)

All tests are **fully offline**: no network, no real `time.sleep` (inject `sleep_fn=lambda _: None` into `EdgarClient`), no real clock. Mirror the existing `tests/ingest/test_edgar.py` style (embedded fixtures + `MagicMock` HTTP client). New tests live in `tests/ingest/edgar/` (create the package `__init__.py`). Fixtures under `tests/ingest/edgar/fixtures/`.

### 4.1 Fixture files to add (`tests/ingest/edgar/fixtures/`)

- `company_tickers.json` — small `{ "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, "1": {...} }` map (the real file's shape).
- `submissions_AAPL.json` — a trimmed `data.sec.gov` submissions doc with `filings.recent` parallel arrays containing a mix of `"4"`, `"SC 13D"`, `"SC 13G/A"`, and unrelated forms (`"8-K"`), with matching `accessionNumber`, `filingDate`, `primaryDocument`.
- `submissions_empty.json` — valid JSON, `filings.recent` arrays empty.
- `sc13d_structured.xml` — a structured 13D with subject company, filer CIK/name, `percentOfClass=8.5`, `aggregateAmountOwned`, event date, `documentType=SC 13D`.
- `sc13g_structured.xml` — a 13G, `percentOfClass=6.2`, `documentType=SC 13G`.
- `sc13da_amendment.xml` — a `SC 13D/A` (amendment) reducing the stake (exit), `percentOfClass=4.1` -> expect `txn_type="S"` / abstain-boundary.
- `sc13_header_only.txt` — old-style plain-text filing with only `<SEC-HEADER>` (SUBJECT COMPANY, FILED BY, CONFORMED SUBMISSION TYPE: SC 13D, CUSIP) and a body line "percent of class: 7.3%".
- `form4_index.htm` — already-covered shape; only add if exercising the index-fallback path of `get_form4_xml`.

### 4.2 Test cases

**Form-4 discovery fix — `test_edgar_discovery.py`:**
1. `get_cik_for_ticker` returns the right zero-padded 10-digit CIK from `company_tickers.json`; returns `None` for an unknown ticker.
2. `search_form4_filings("AAPL")` against `submissions_AAPL.json` returns only `form == "4"` rows, newest-first, each with non-empty `accession`, `cik`, and `primary_document`. **(This is the regression test for the bug — asserts `accession`/`cik` are non-empty.)**
3. `search_form4_filings` against `submissions_empty.json` returns `[]` (no crash).
4. `search_form4_filings` for an unresolvable ticker returns `[]`.
5. `get_form4_xml(acc, cik, primary_document="x.xml")` fetches the doc URL directly and **does not** request the index page (assert the mock was called once with the doc URL).
6. `get_form4_xml` with `primary_document=None` falls back to the index-scrape path (existing behavior preserved).
7. End-to-end (mocked HTTP): `search_form4_filings -> get_form4_xml -> parse_form4 -> normalize` yields ≥1 `RawFiling` with `source="form4"`.

**Graceful skip — `test_edgar_disabled.py`:**
8. `EdgarClient.from_config_or_none(config_with_empty_ua)` returns `None` and logs exactly one warning (capture with `caplog`/structlog capture).
9. `EdgarClient.from_config_or_none(config_with_ua)` returns a constructed client.
10. Direct `EdgarClient(config_with_empty_ua)` still raises `ValueError` (back-compat).

**13D/13G parse — `test_sc13_parser.py`:**
11. `parse_sc13(sc13d_structured.xml, "AAPL", acc, schedule="13D")` -> one row: `schedule="13D"`, `is_activist=True`, `percent_of_class==8.5`, `transaction_code=="P"`, `person_id`==filer CIK, `ticker=="AAPL"`, tz-aware `filing_ts`.
12. `parse_sc13(sc13g_structured.xml, …, schedule="13G")` -> `is_activist=False`, `percent_of_class==6.2`.
13. `parse_sc13(sc13da_amendment.xml, …)` -> `is_amendment=True`; stake dropped to 4.1% -> `transaction_code=="S"`.
14. `parse_sc13(sc13_header_only.txt, …)` (header fallback) -> extracts subject, filer, schedule, cusip; `percent_of_class==7.3`.
15. Malformed/empty input -> returns `[]` (no exception).

**13D/13G normalize — `test_sc13_normalize.py`:**
16. `normalize_sc13` maps a 13D row to a `RawFiling` with `source="form13d"`, `txn_type="P"`, `amount_low is None`, `amount_high is None`, `is_10b5_1 is False`, and `raw_json` containing `schedule`/`percent_of_class`/`is_activist`.
17. A `<5%` non-amendment row is dropped.
18. An amendment row sets `is_amendment=True` and survives normalization.

**13D/13G discovery — in `test_edgar_discovery.py`:**
19. `search_sc13_filings("AAPL")` returns only the `SC 13D*`/`SC 13G*` rows from `submissions_AAPL.json`, each tagged with `schedule` and `is_amendment`.

### 4.3 Test-isolation requirements (honor INTERFACES §11)

- No `datetime.now()` in tests — pass explicit timestamps/fixtures.
- Inject `sleep_fn=lambda _: None` into every `EdgarClient` to neutralize rate-limiting (no real sleeps).
- Inject a `MagicMock` `httpx.Client` (or a `respx` transport) — zero real network.

---

## 5. WIRING CONTRACT (FROZEN)

The EDGAR lane is **data-producing**: it populates the `filings` table; the engine reads that table via `detect_signals`. There is therefore **no `build_*_opinion()` function exported from `arbiter/ingest/edgar`**. The frozen contract has two parts.

### 5.1 The `source` strings written into `filings` (frozen)

| Source | `filings.source` value | `txn_type` values | advisor_id (engine owner registers) |
|---|---|---|---|
| Form 4 | `"form4"` (unchanged) | `"P"`, `"S"` | `A1.insider` (existing) |
| 13D/13G | **`"form13d"`** (new) | `"P"` (new/increased stake), `"S"` (exit/reduction) | **`A1.activist`** (new) |

`raw_json` for `form13d` rows carries: `schedule` (`"13D"`/`"13G"`), `is_activist` (bool), `percent_of_class` (float|None), `cusip` (str|None), `aggregate_amount` (float|None).

### 5.2 The ingest entry points the runner/engine owner calls (frozen signatures)

These live in `arbiter/ingest/edgar/` and are the mechanical handoff:

```python
# arbiter/ingest/edgar/client.py
from arbiter.config import Config
from arbiter.ingest.edgar.client import EdgarClient

class EdgarClient:
    @classmethod
    def from_config_or_none(cls, config: Config) -> "EdgarClient | None":
        """Return a client, or None (logging one WARNING) when
        config.edgar_user_agent is empty/whitespace. Never raises on unset UA."""

    def get_cik_for_ticker(self, ticker: str) -> str | None: ...

    def search_form4_filings(
        self, ticker: str, *, count: int = 20
    ) -> list[dict]:
        """-> [{cik, accession, filed_at, primary_document}], newest-first.
        Empty list when ticker unresolvable or no Form-4 filings."""

    def get_form4_xml(
        self, accession: str, cik: str, *, primary_document: str | None = None
    ) -> str: ...

    def search_sc13_filings(
        self, ticker: str, *, count: int = 20
    ) -> list[dict]:
        """-> [{cik, accession, filed_at, primary_document, schedule, is_amendment}]."""

    def get_sc13_doc(
        self, accession: str, cik: str, *, primary_document: str | None = None
    ) -> str: ...
```

```python
# arbiter/ingest/edgar/__init__.py  (public surface)
from arbiter.ingest.edgar import (
    EdgarClient,
    parse_form4,            # unchanged: (xml, ticker, accession) -> list[dict]
    normalize,              # unchanged: (list[dict]) -> list[RawFiling]
    parse_sc13,             # NEW: (raw_text, ticker, accession, *, schedule) -> list[dict]
    normalize_sc13,         # NEW: (list[dict]) -> list[RawFiling]
    RawFiling,
)
```

`RawFiling` (from `normalize.py`) is the **same TypedDict** for both Form-4 and 13D/G rows; both are persisted with the existing `arbiter.ingest.writer.write_filing(conn, raw, clock)`.

### 5.3 Opinion-mapping constants the engine owner will use (frozen)

For `A1.activist` (engine owner adds the `emit_opinion` branch in `arbiter/signals/emit.py` and the detector branch in `arbiter/signals/detection.py`):
- `advisor_id = "A1.activist"`, `confidence_source = ConfidenceSource.MODELED`, `horizon_days = 180` (LONG).
- conviction = `0.70` (13D) or `0.35` (13G), `+min(percent_of_class/50.0, 0.30)`, clamped to (0.01, 1.0].
- stance sign: `+` for `txn_type=="P"`, `−` for `txn_type=="S"`.

---

## 6. FOR WAVE 2 (engine owner) — NOT designed here, listed for handoff

These require edits **outside** `arbiter/ingest/edgar/**` and are explicitly **not** part of this lane's implementation:

1. **`arbiter/signals/detection.py`:** add a `source = 'form13d'` SELECT branch and a `_detect_activist_stake` sub-detector emitting `Signal(source="form13d", …)`. (The cluster/single Form-4 detectors are unchanged.) A single 13D/G filing is itself a signal (no clustering needed), analogous to `_detect_single_insider`.
2. **`arbiter/signals/emit.py`:** add `if signal.source == "form13d": advisor_id="A1.activist"; horizon_days=180`, and the conviction/sign math from §5.3. Honor the existing abstain rules.
3. **`arbiter/engine.py`:** add `_build_a1_activist_fn` mirroring `_build_a1_insider_fn` (engine.py:135), filtering `s.source == "form13d"`, and register `A1.activist` in the advisor map / registry.
4. **`arbiter/ingest/runner.py`:** add a `_ingest_sc13` pass (mirror `_ingest_form4`, calling `search_sc13_filings -> get_sc13_doc -> parse_sc13 -> normalize_sc13 -> write_filing`); add `"form13d"` to the default `sources`. **Optional:** switch the form4 guard to `EdgarClient.from_config_or_none`. The runner's existing UA guard (runner.py:197) already satisfies "inert, not crash", so this is not required.
5. **Learning loop / calibration:** `A1.activist` participates automatically once registered; no special wiring beyond registration (it graduates via the same significance-gated path as the other A1 advisors).

---

## 7. Constraints honored

- No `datetime.now()` anywhere in the lane (timestamps from parsed event/filing dates; runner passes `clock`).
- `None`-preservation for undisclosed dollar amounts (no fabricated midpoints) — consistent with the Form-4 `_float_or_none` rule.
- Unset `EDGAR_USER_AGENT` -> inert with one WARNING; never crashes; Congress still runs.
- All tests offline, no real sleeps, injected HTTP mock.
- No `filings` migration required (`source` is unconstrained TEXT).
- Strict ownership: every code change is in `arbiter/ingest/edgar/**` or `tests/ingest/edgar/**`; everything else is under §6.
