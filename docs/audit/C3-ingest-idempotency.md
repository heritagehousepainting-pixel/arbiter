# C3 — Ingest Idempotency & Amendment/Supersede Handling (READ-ONLY audit)

- **Lane:** C3 — write/dedup correctness of ingestion (NOT parsing; that is C1/C2)
- **Date:** 2026-06-19
- **Auditor scope:** ingest writer/runner, `arbiter/db/helpers.py` (`insert_row`/`supersede_row`/`supersede_rows`), accession schemes (`H-{docid}-{i}` / `S-{uuid}-{i}`), `txn_idx`, the `UNIQUE(accession, txn_idx)` index, Senate amendment supersession ordering, House amendment wiring.
- **Files reviewed:** `arbiter/ingest/writer.py`, `arbiter/ingest/runner.py`, `arbiter/db/helpers.py`, `arbiter/db/connection.py`, `arbiter/ingest/congress/__init__.py`, `arbiter/ingest/congress/normalize.py`, `arbiter/ingest/congress/senate.py`, `arbiter/ingest/congress/index.py`, `arbiter/ingest/congress/parser.py`, `arbiter/ingest/congress/ptr_pdf.py`, migrations `008b/008c/021`.

## VERDICT

**PASS WITH CONCERNS.** Core idempotency is sound and DB-enforced: re-running ingest is genuinely a no-op (verified — `(accession, txn_idx)` check-then-skip backed by a `UNIQUE` partial index in migration 021). Amendment supersession is atomic and supersedes ALL priors exactly once via `supersede_rows` (no double-count, no both-active for Senate). The supersede transaction is correctly atomic (SAVEPOINT + commit/rollback, WAL-safe). The two real gaps: **(1) House amendment detection is NOT wired** — confirmed, and it is worse than "not detected": House amendment filings are *filtered out entirely* before they reach the writer, so a corrected House PTR silently leaves the stale original active (P1). **(2) Amendment supersedes by `(ticker, person_id)` over the whole history**, which over-supersedes unrelated still-valid filings for the same person/ticker (P1). A check-then-insert TOCTOU race exists but is contained by the runner's per-filing try/except and the UNIQUE index (P2 — only matters under concurrent ingest, which is not the current single-process model).

---

## FINDINGS

### P1 — House amendments are filtered out, never superseded — `arbiter/ingest/congress/index.py:205` + `arbiter/ingest/congress/ptr_pdf.py:44` — why — recommended action

The live House pipeline is `fetch_house_ptrs` → `filter_ptrs(... electronic_only=True)` → `fetch_ptr_pdf` → `parse_ptr` → `to_raw_filings`. `filter_ptrs` keeps **only `filing_type == "P"`** (`index.py:205`). House amendment filings carry a different FilingType code in the FD index, so they are dropped before they ever reach a `Transaction`. Even if one slipped through, `parse_ptr` builds `Transaction(is_amendment=False)` by default (`ptr_pdf.py:44`) and nothing in the House path ever sets it True — `IndexRecord` (`index.py:19`) does not even carry an amendment flag into the PTR pipeline. (Note: the *old* JSON `parser.py:242-245` DOES detect `doc_type == "amendment"`, but that path is unused by the live runner.)

**Risk:** when a Representative files a correction/amendment to a prior PTR, Arbiter keeps the **original (now-wrong) transaction active** and never sees the correction. This is a silent data-integrity defect: stale buy/sell amounts and directions persist and feed signals. Senate corrections are handled; House are not — an asymmetric blind spot.

**Recommended action:** (a) carry the House FilingType (and any "C"/amendment code) through `IndexRecord` into the PTR pipeline and set `Transaction.is_amendment` accordingly; (b) stop dropping amendment FilingTypes in `filter_ptrs`, or add a parallel keep-set for amendment codes; (c) add a test asserting a House amendment supersedes its original. Until done, document the gap loudly (it is currently only implied by the `# House always False` comment).

### P1 — Amendment supersedes ALL prior filings for (ticker, person_id), not the specific original — `arbiter/ingest/writer.py:74-91, 222-232` — why — recommended action

`_find_all_prior_filings` selects **every** non-superseded filing matching `ticker = ? AND person_id = ?` with `filing_ts <= ?` (`_ALL_PRIOR_FILINGS_QUERY_AMENDMENT`, lines 85-91), and `write_filing` supersedes all of them (lines 222-232). A PTR amendment typically corrects ONE specific prior report, but a member commonly files many *independent* purchases/sales of the same ticker over time (each its own legitimate transaction). An amendment to one of them will mark **all** of that member's prior same-ticker filings superseded, collapsing a multi-transaction history into the single amendment row.

**Risk:** under-counting / data loss — legitimate distinct transactions for the same (ticker, person) silently disappear from the active set when any one amendment arrives. The module docstring frames "supersede ALL priors" as the *fix* for double-counting in multi-amendment chains, but the chosen key (ticker+person, no original-filing linkage) is too coarse: it conflates "amendment chain for one report" with "all history for this person+ticker."

**Recommended action:** link an amendment to the specific filing it corrects (e.g. the source doc-id / original accession of the amended report) and supersede only that chain. If the upstream amendment payload genuinely lacks a back-reference to the original, narrow the match window (e.g. only supersede priors within the amendment report's own transaction set / same disclosure period) and document the residual risk. Add a test with two independent same-ticker purchases + one amendment proving only the targeted one is superseded.

### P2 — Check-then-insert TOCTOU on `(accession, txn_idx)` — `arbiter/ingest/writer.py:177-190, 252-253` — why — recommended action

Idempotency relies on a SELECT (`_accession_txn_exists` / `_accession_exists`) followed by a separate `insert_row`. Between the two there is no lock. Under concurrent ingest of the same accession, both callers can pass the existence check and both attempt the insert; the second hits `UNIQUE constraint failed` (verified the index raises `IntegrityError`). `insert_row` has no `try/except` (`helpers.py:46-66`), so the exception propagates — it is caught by the runner's per-filing `except Exception` (`runner.py:292-301, 411-420`) and counted as a skip/error rather than recognised as a benign duplicate.

**Risk:** today this is theoretical — ingest is single-process/single-threaded (one `run_ingest` per cycle), so the window is not exercised. But if a second ingest worker, a daemon overlap, or a retry ever runs concurrently, legitimate re-runs would log spurious write-errors (noise, inflated error counts) rather than clean idempotent skips. Correctness of the *data* is preserved by the UNIQUE index; only the accounting/observability degrades.

**Recommended action:** make the writer treat a `(accession, txn_idx)` `IntegrityError` as a duplicate: catch it, re-query for the existing id, and return it (so the runner counts it as a skip, not an error). Cheaper alternative: `INSERT ... ON CONFLICT(accession, txn_idx) DO NOTHING` then read back the id. Either makes the dedup race-safe and removes the dependency on the runner's catch-all.

### P2 — Multi-transaction Form 4 has a partial-write window (idempotent on re-run, but transiently inconsistent) — `arbiter/ingest/runner.py:263-291` + `arbiter/db/helpers.py:62-64` — why — recommended action

One Form 4 accession yields several `RawFiling` dicts (one per `txn_idx`), each written by a separate `write_filing` → `insert_row`, and `insert_row` **commits per row** (`helpers.py:62-64`). A crash midway through a multi-transaction filing leaves some `txn_idx` rows committed and the rest missing.

**Risk:** the partial state does NOT create duplicates or both-active rows (each `txn_idx` is unique), and the next ingest run re-fetches the same accession and inserts the missing `txn_idx` rows idempotently — so it self-heals. The only window is *between* the crash and the next run, during which a filing's transactions are incomplete and could feed a partial signal. Low severity given the disclosure-cadence (not real-time) and self-healing re-runs.

**Recommended action:** optionally wrap all `txn_idx` rows of a single accession in one transaction (commit once per accession) so a filing is all-or-nothing. Acceptable to leave as-is given self-healing, but document the window. Note `_count_filings` before/after each write (`runner.py:280-282, 399-401`) is an O(rows) `SELECT count(*)` per filing — correctness is fine but it is an N×full-scan; see opportunities.

### P3 — `supersede_rows` records only `old_ids[0]` as `supersedes_id`, losing the back-reference to the other superseded rows — `arbiter/db/helpers.py:164, 196` — why — recommended action

When an amendment supersedes N priors, the new row stores `supersedes_id = old_ids[0]` (the most-recent prior) only. The remaining N-1 superseded rows have `is_superseded = 1` but no forward pointer to the row that replaced them. The audit event (`writer.py:233-243`) does capture the full `superseded_ids` list, so provenance is recoverable from the audit log, not the table.

**Risk:** purely a lineage/auditability gap — reconstructing "which row replaced this superseded one" requires the audit log, not the `filings` table. No double-count or correctness impact.

**Recommended action:** acceptable as-is given the audit-log capture; if table-level lineage is wanted later, add a `superseded_by_id` column stamped on each old row during the flip.

### PASS — Re-run idempotency is genuine and DB-enforced

Verified: `(accession, txn_idx)` dedup (`writer.py:177-190`) short-circuits before any insert and returns the existing id; the runner's before/after count then classifies it as a skip (`runner.py:286-291, 405-410`). Backed by `UNIQUE INDEX idx_filings_accession_txn` (migration `021`, partial: `WHERE accession IS NOT NULL AND txn_idx IS NOT NULL`) — confirmed it raises `IntegrityError` on a duplicate pair. Congress filings without a real accession get a deterministic synthetic key (`runner._congress_accession`, line 428-436: `CONG-sha256(person:ticker:filing_ts:txn_idx)`) and the normalizer uses the **input enumerate position** for `txn_idx`/accession (`normalize.py:226-229, 364-370`) so filter changes do not shift keys — both make re-runs stable. 21/21 `test_writer.py` pass.

### PASS — Supersede transaction is atomic and WAL-safe

`supersede_row`/`supersede_rows` (`helpers.py:113-180`) wrap the insert-of-correcting-row + every `is_superseded = 1` flip in a single `SAVEPOINT ... RELEASE` then `commit()`, with `ROLLBACK TO` on exception. This is correct regardless of `isolation_level` (default `''` autocommit-deferred, confirmed) or WAL mode — SAVEPOINTs nest safely whether or not a parent transaction is open. A crash between the insert and the flips cannot leave both rows active. This is the documented "only UPDATE in the codebase" and it is the only place `is_superseded` is mutated.

### PASS — Senate amendment ordering (non-amendments written first) is correct and necessary

The writer only supersedes priors **already in the DB** (`writer.py:222-232`), so when an original and its amendment land in the same batch, the original must be written first. `fetch_senate_ptrs` enforces this with a **stable** sort `capped_uuids.sort(key=lambda uid: bool(is_amendment))` (`__init__.py:242`) — `False` (original) sorts before `True` (amendment), and stability preserves date-desc order within each group. The amendment query uses `filing_ts <=` (not `<`) so a same-day original is caught (`writer.py:81-91`), and excludes the amendment's own accession (`AND accession != ?`) to prevent self-supersede. Correct.

---

## OPPORTUNITIES TO ADD

- **Race-safe dedup:** convert the writer to `INSERT ... ON CONFLICT(accession, txn_idx) DO NOTHING` + read-back, eliminating the TOCTOU (P2) and the reliance on the runner's catch-all to swallow `IntegrityError`.
- **Cheaper write accounting:** replace the per-filing `SELECT count(*) FROM filings` before/after (`runner.py:280-282, 399-401`) with the writer returning a 3-state result (`inserted` / `duplicate` / `excluded`); avoids two full-table counts per filing and removes a fragile "after > before" heuristic.
- **House amendment coverage test + plumbing** (ties to P1#1): a fixture House FilingType amendment code that proves it both reaches the writer and supersedes its original.
- **Original-linked supersession** (ties to P1#2): persist the amended report's source doc-id on each filing so amendments supersede the exact prior chain rather than all same-(ticker,person) history; add a regression test with independent same-ticker transactions.
- **Per-accession transaction** for multi-`txn_idx` Form 4 (ties to P2#2): one commit per accession so a filing is all-or-nothing, closing the transient partial-write window.
- **Concurrency contract doc:** state explicitly that ingest is single-writer; if the daemon ever overlaps a manual ingest, the TOCTOU and per-row commits become live. A `BEGIN IMMEDIATE` / advisory single-writer lock around `run_ingest` would make the assumption enforced rather than assumed.
