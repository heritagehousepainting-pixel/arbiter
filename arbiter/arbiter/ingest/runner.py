"""Ingest runner — Lane 5 orchestration entry point.

Pulls real SEC Form 4 + Congress disclosures and writes them to the DB.

The adapters (edgar, congress) are fully built and tested in isolation.
This module is the ONLY place that calls them in sequence and writes results
to the ``filings`` table via the writer.

Design constraints (INTERFACES.md §11)
---------------------------------------
- No ``datetime.now()`` — all timestamps from ``clock`` callable.
- Fault-isolated per filing: one malformed filing logs + continues.
- Idempotent re-runs: the writer dedups on (accession, txn_idx).
- ``edgar_user_agent`` empty → form4 skipped with a warning; Congress still runs.
- ``sources`` controls which adapters are activated.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Sequence

import structlog

from arbiter.config import Config
from arbiter.data.activist_filers import ACTIVIST_FILERS
from arbiter.data.sectors import covered_tickers
from arbiter.ingest.congress import CongressClient, fetch_house_ptrs, fetch_senate_ptrs
from arbiter.ingest.edgar import normalize_sc13, parse_sc13
from arbiter.ingest.edgar.client import EdgarClient
from arbiter.ingest.edgar.cusip_resolver import resolve_cusip
from arbiter.ingest.edgar.normalize import normalize as _edgar_normalize
from arbiter.ingest.edgar.parser import parse_form4
from arbiter.ingest.identity.resolver import resolve_person
from arbiter.ingest.writer import write_filing

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Default watchlist (used when callers do not supply tickers)
# ---------------------------------------------------------------------------
#
# Derived from the maintained sector map (``data.sectors``) — the same
# 11-sector, ~136-name universe that backs the per-sector risk cap.  This is the
# shared ticker universe for THREE consumers: form4 (insider) ingest, form13d
# (activist) subject-search, and the A3 news pipeline (which imports this name
# from ``engine/_engine.py``).  Keeping it equal to ``covered_tickers()`` means
# the watchlist ⊆ sector-map invariant (test_sectors) holds by construction and
# can never drift.  Sorted for deterministic ingest order.

_DEFAULT_WATCHLIST: tuple[str, ...] = tuple(sorted(covered_tickers()))


# ---------------------------------------------------------------------------
# IngestSummary
# ---------------------------------------------------------------------------

@dataclass
class SourceSummary:
    """Per-source breakdown included in IngestSummary."""
    n_fetched: int = 0
    n_written: int = 0
    n_skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class IngestSummary:
    """Aggregate result of a run_ingest call.

    Attributes
    ----------
    sources:
        Tuple of source names that were *requested* (e.g. ``("form4", "congress")``).
        A source listed here may have been skipped (e.g. no edgar_user_agent) —
        check ``per_source`` for the actual outcome.
    n_fetched:
        Total raw filings fetched across all sources.
    n_written:
        Total filings successfully written to the DB.
    n_skipped:
        Total filings skipped (duplicate, 10b5-1, malformed, etc.).
    errors:
        Human-readable error strings from any per-filing failures.
    per_source:
        Dict of ``source_name -> SourceSummary`` for granular introspection.
    notes:
        Free-text notes (e.g. "form4 skipped: edgar_user_agent not configured").
    """
    sources: tuple[str, ...]
    n_fetched: int = 0
    n_written: int = 0
    n_skipped: int = 0
    errors: list[str] = field(default_factory=list)
    per_source: dict[str, SourceSummary] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_ingest(
    config: Config,
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    sources: Sequence[str] = ("form4", "form13d", "congress", "form13f"),
    tickers: list[str] | None = None,
    lookback_days: int = 7,
) -> IngestSummary:
    """Pull SEC Form 4 + Congress disclosures and write them to the DB.

    Parameters
    ----------
    config:
        Loaded ``Config`` instance.  Uses ``config.edgar_user_agent`` for EDGAR.
    conn:
        Open SQLite connection with all migrations applied (001_core + 008_identity).
    clock:
        Callable returning a tz-aware ISO-8601 UTC timestamp string.  Never calls
        ``datetime.now()`` directly (INTERFACES.md §11.1).
    sources:
        Which adapters to activate.  Defaults to both ``"form4"`` and ``"congress"``.
        Pass ``("congress",)`` to skip EDGAR entirely.
    tickers:
        Ticker list for Form 4 searches.  ``None`` falls back to the built-in
        default watchlist (top-10 S&P names).
    lookback_days:
        How many calendar days back to search (used to compute ``as_of`` window
        for Congress; EDGAR always fetches the most recent ``count=20`` filings
        per ticker so it is less relevant there).

    Returns
    -------
    IngestSummary with aggregate + per-source counts.

    Notes
    -----
    - If ``config.edgar_user_agent`` is empty or whitespace, form4 is skipped
      with a logged WARNING and a note in the summary — Congress still runs.
    - Each filing is fault-isolated: one exception logs + increments n_skipped,
      then the loop continues.
    - Re-runs are idempotent: the writer detects (accession, txn_idx) duplicates
      and returns the existing id without inserting.
    """
    sources_tuple = tuple(sources)
    summary = IngestSummary(sources=sources_tuple)

    # Compute the information timestamp from the clock (no datetime.now).
    as_of = _parse_clock_to_datetime(clock())
    start_date = (as_of - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    end_date = as_of.strftime("%Y-%m-%d")

    if "form4" in sources_tuple:
        _ingest_form4(config, conn=conn, clock=clock, summary=summary, tickers=tickers)

    if "form13d" in sources_tuple:
        # Two discovery paths feed the activist channel: subject-search (13D
        # filed AGAINST a watchlist ticker) and filer-search (what a known
        # activist filed against ANYONE).  Union, deduped by the writer.
        _ingest_sc13(config, conn=conn, clock=clock, summary=summary, tickers=tickers)
        _ingest_sc13_by_filer(config, conn=conn, clock=clock, summary=summary)

    if "congress" in sources_tuple:
        _ingest_congress(
            conn=conn,
            clock=clock,
            summary=summary,
            as_of=as_of,
            start_date=start_date,
            end_date=end_date,
        )

    if "form13f" in sources_tuple:
        _ingest_form13f(config, conn=conn, clock=clock, summary=summary)

    # Roll up per-source totals into aggregate fields.
    for src_summary in summary.per_source.values():
        summary.n_fetched += src_summary.n_fetched
        summary.n_written += src_summary.n_written
        summary.n_skipped += src_summary.n_skipped
        summary.errors.extend(src_summary.errors)

    log.info(
        "run_ingest.complete",
        sources=sources_tuple,
        n_fetched=summary.n_fetched,
        n_written=summary.n_written,
        n_skipped=summary.n_skipped,
        n_errors=len(summary.errors),
        notes=summary.notes,
    )
    return summary


# ---------------------------------------------------------------------------
# Private: form4 ingestion
# ---------------------------------------------------------------------------

def _ingest_form4(
    config: Config,
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    summary: IngestSummary,
    tickers: list[str] | None,
) -> None:
    """Ingest SEC Form 4 filings for each ticker in the watchlist."""
    src = SourceSummary()
    summary.per_source["form4"] = src

    # Guard: edgar_user_agent required.  Skip form4 (not a crash) if absent.
    if not config.edgar_user_agent or not config.edgar_user_agent.strip():
        msg = (
            "form4 skipped: Config.edgar_user_agent is empty. "
            "Set EDGAR_USER_AGENT or [edgar] user_agent in arbiter.toml."
        )
        log.warning("run_ingest.form4_skipped_no_user_agent")
        summary.notes.append(msg)
        src.errors.append(msg)
        return

    watchlist = tickers if tickers else list(_DEFAULT_WATCHLIST)

    try:
        client = EdgarClient(config=config)
    except Exception as exc:
        msg = f"form4: failed to create EdgarClient: {exc}"
        log.error("run_ingest.form4_client_error", error=str(exc))
        src.errors.append(msg)
        return

    try:
        for ticker in watchlist:
            _ingest_form4_ticker(
                ticker=ticker,
                client=client,
                conn=conn,
                clock=clock,
                src=src,
                parse_form4_fn=parse_form4,
                edgar_normalize=_edgar_normalize,
            )
    finally:
        client.close()


def _ingest_form4_ticker(
    ticker: str,
    client: object,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    src: SourceSummary,
    parse_form4_fn: object,
    edgar_normalize: object,
) -> None:
    """Ingest all recent Form 4 filings for one ticker."""
    try:
        filing_refs = client.search_form4_filings(ticker)
    except Exception as exc:
        msg = f"form4/{ticker}: search failed: {exc}"
        log.warning("run_ingest.form4_search_error", ticker=ticker, error=str(exc))
        src.errors.append(msg)
        return

    for ref in filing_refs:
        accession = ref.get("accession", "")
        cik = ref.get("cik", "")
        if not accession or not cik:
            src.n_skipped += 1
            continue

        src.n_fetched += 1
        try:
            xml_text = client.get_form4_xml(accession, cik)
            parsed_rows = parse_form4_fn(xml_text, ticker, accession)
            raw_filings = edgar_normalize(parsed_rows)

            for raw in raw_filings:
                # Resolve person identity before writing.
                hints: dict = {}
                if raw.get("person_id"):
                    hints["person_id"] = raw["person_id"]

                try:
                    person_id = resolve_person(
                        raw["person_name"],
                        "form4",
                        hints,
                        conn,
                        clock,
                    )
                    raw = dict(raw)
                    raw["person_id"] = person_id

                    before = _count_filings(conn)
                    fid = write_filing(conn, raw, clock)
                    after = _count_filings(conn)
                    if fid is None:
                        # 10b5-1 exclusion — writer returned None.
                        src.n_skipped += 1
                    elif after > before:
                        # A new row was actually inserted.
                        src.n_written += 1
                    else:
                        # Duplicate — writer returned existing id, no new row.
                        src.n_skipped += 1
                except Exception as exc:
                    msg = f"form4/{ticker}/{accession}: write error: {exc}"
                    log.warning(
                        "run_ingest.form4_write_error",
                        ticker=ticker,
                        accession=accession,
                        error=str(exc),
                    )
                    src.errors.append(msg)
                    src.n_skipped += 1

        except Exception as exc:
            msg = f"form4/{ticker}/{accession}: fetch/parse error: {exc}"
            log.warning(
                "run_ingest.form4_filing_error",
                ticker=ticker,
                accession=accession,
                error=str(exc),
            )
            src.errors.append(msg)
            src.n_skipped += 1
            # Continue to the next filing — fault-isolated.


# ---------------------------------------------------------------------------
# Private: Schedule 13D/13G ingestion (form13d)
# ---------------------------------------------------------------------------

def _ingest_sc13(
    config: Config,
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    summary: IngestSummary,
    tickers: list[str] | None,
) -> None:
    """Ingest SEC Schedule 13D/13G filings for each ticker in the watchlist.

    Mirrors :func:`_ingest_form4` exactly, including the UA-inert guard: an
    empty ``edgar_user_agent`` skips form13d with a warning (not a crash) —
    other requested sources still run.
    """
    src = SourceSummary()
    summary.per_source["form13d"] = src

    # Guard: edgar_user_agent required.  Skip form13d (not a crash) if absent.
    if not config.edgar_user_agent or not config.edgar_user_agent.strip():
        msg = (
            "form13d skipped: Config.edgar_user_agent is empty. "
            "Set EDGAR_USER_AGENT or [edgar] user_agent in arbiter.toml."
        )
        log.warning("run_ingest.form13d_skipped_no_user_agent")
        summary.notes.append(msg)
        src.errors.append(msg)
        return

    watchlist = tickers if tickers else list(_DEFAULT_WATCHLIST)

    try:
        client = EdgarClient(config=config)
    except Exception as exc:
        msg = f"form13d: failed to create EdgarClient: {exc}"
        log.error("run_ingest.form13d_client_error", error=str(exc))
        src.errors.append(msg)
        return

    try:
        for ticker in watchlist:
            _ingest_sc13_ticker(ticker, client, conn, clock, src)
    finally:
        client.close()


def _ingest_sc13_ticker(
    ticker: str,
    client: object,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    src: SourceSummary,
) -> None:
    """Ingest all recent Schedule 13D/13G filings for one ticker (fault-isolated)."""
    try:
        refs = client.search_sc13_filings(ticker)
    except Exception as exc:
        msg = f"form13d/{ticker}: search failed: {exc}"
        log.warning("run_ingest.form13d_search_error", ticker=ticker, error=str(exc))
        src.errors.append(msg)
        return

    for ref in refs:
        accession = ref.get("accession", "")
        cik = ref.get("cik", "")
        schedule = ref.get("schedule", "13G")
        if not accession or not cik:
            src.n_skipped += 1
            continue

        src.n_fetched += 1
        try:
            doc = client.get_sc13_doc(
                accession, cik, primary_document=ref.get("primary_document")
            )
            parsed = parse_sc13(doc, ticker, accession, schedule=schedule)
            raws = normalize_sc13(parsed)

            for raw in raws:
                try:
                    hints = {"person_id": raw["person_id"]} if raw.get("person_id") else {}
                    person_id = resolve_person(
                        raw["person_name"], "form13d", hints, conn, clock
                    )
                    raw = dict(raw)
                    raw["person_id"] = person_id

                    before = _count_filings(conn)
                    fid = write_filing(conn, raw, clock)
                    after = _count_filings(conn)
                    if fid is None:
                        src.n_skipped += 1
                    elif after > before:
                        src.n_written += 1
                    else:
                        src.n_skipped += 1
                except Exception as exc:
                    msg = f"form13d/{ticker}/{accession}: write error: {exc}"
                    log.warning(
                        "run_ingest.form13d_write_error",
                        ticker=ticker,
                        accession=accession,
                        error=str(exc),
                    )
                    src.errors.append(msg)
                    src.n_skipped += 1

        except Exception as exc:
            msg = f"form13d/{ticker}/{accession}: fetch/parse error: {exc}"
            log.warning(
                "run_ingest.form13d_filing_error",
                ticker=ticker,
                accession=accession,
                error=str(exc),
            )
            src.errors.append(msg)
            src.n_skipped += 1
            # Continue to the next filing — fault-isolated.


# ---------------------------------------------------------------------------
# Private: Schedule 13D by-filer ingestion (named activists)
# ---------------------------------------------------------------------------

def _resolve_subject_ticker(
    client: object,
    conn: sqlite3.Connection,
    row: dict,
    asset_lookup: "Callable[[], dict[str, str]]",
    clock: Callable[[], str],
) -> str | None:
    """Resolve the subject ticker for a filer-discovered 13D (safety-first).

    Priority: exact subject-CIK reverse lookup, then CUSIP resolution (cache →
    seed → Alpaca issuer-name match).  Returns ``None`` (→ DROP, never trade on
    a guess) when neither resolves with confidence — consistent with the 13F
    CUSIP-drop policy.
    """
    subj_cik = (row.get("subject_cik") or "").strip()
    if subj_cik:
        t = client.get_ticker_for_cik(subj_cik.zfill(10))  # type: ignore[attr-defined]
        if t:
            return t.upper()
    cusip = (row.get("cusip") or "").strip()
    if cusip:
        t = resolve_cusip(
            conn,
            cusip,
            row.get("subject_name") or "",
            asset_lookup=asset_lookup,
            now_iso=clock(),
        )
        if t:
            return t.upper()
    return None


def _ingest_sc13_by_filer(
    config: Config,
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    summary: IngestSummary,
) -> None:
    """Ingest 13D/13G filings discovered by tracked-activist filer CIK.

    Mirrors ``_ingest_form13f``'s shape: iterate a static roster
    (``ACTIVIST_FILERS``), read each filer's own submissions for 13D/13G, fetch
    + parse each doc, resolve the subject ticker, and write keepers.  Counts go
    in a separate ``form13d_activist`` per-source bucket (still ``source=
    "form13d"`` rows in the DB), so the subject-search vs filer-search split is
    visible.  UA-empty guard skips this path (not a crash); other sources run.
    """
    src = SourceSummary()
    summary.per_source["form13d_activist"] = src

    if not config.edgar_user_agent or not config.edgar_user_agent.strip():
        msg = (
            "form13d_activist skipped: Config.edgar_user_agent is empty. "
            "Set EDGAR_USER_AGENT or [edgar] user_agent in arbiter.toml."
        )
        log.warning("run_ingest.form13d_activist_skipped_no_user_agent")
        summary.notes.append(msg)
        src.errors.append(msg)
        return

    asset_lookup = _alpaca_asset_lookup(config)

    try:
        client = EdgarClient(config=config)
    except Exception as exc:
        msg = f"form13d_activist: failed to create EdgarClient: {exc}"
        log.error("run_ingest.form13d_activist_client_error", error=str(exc))
        src.errors.append(msg)
        return

    try:
        for filer in ACTIVIST_FILERS:
            _ingest_sc13_filer_one(
                filer=filer,
                client=client,
                conn=conn,
                clock=clock,
                src=src,
                asset_lookup=asset_lookup,
            )
    finally:
        client.close()


def _ingest_sc13_filer_one(
    *,
    filer: object,
    client: object,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    src: SourceSummary,
    asset_lookup: "Callable[[], dict[str, str]]",
) -> None:
    """Ingest one activist filer's recent 13D/13G filings (fault-isolated)."""
    cik = filer.cik  # type: ignore[attr-defined]
    try:
        refs = client.search_sc13_by_filer(cik)  # type: ignore[attr-defined]
    except Exception as exc:
        msg = f"form13d_activist/{cik}: search failed: {exc}"
        log.warning("run_ingest.form13d_activist_search_error", cik=cik, error=str(exc))
        src.errors.append(msg)
        return

    for ref in refs:
        accession = ref.get("accession", "")
        if not accession:
            src.n_skipped += 1
            continue

        src.n_fetched += 1
        try:
            doc = client.get_sc13_doc(  # type: ignore[attr-defined]
                accession, cik, primary_document=ref.get("primary_document")
            )
            parsed = parse_sc13(
                doc, "", accession, schedule=ref.get("schedule", "13D")
            )

            for row in parsed:
                ticker = _resolve_subject_ticker(client, conn, row, asset_lookup, clock)
                if not ticker:
                    # Unresolved subject — drop, never trade on a guess.
                    src.n_skipped += 1
                    continue

                row = dict(row)
                row["ticker"] = ticker
                # Identity = the activist filer.  Prefer the parsed reporting
                # person; fall back to the roster name + filer CIK.
                person_name = row.get("person_name") or filer.name  # type: ignore[attr-defined]
                hints = {"person_id": row.get("person_id") or cik}
                try:
                    person_id = resolve_person(
                        person_name, "form13d", hints, conn, clock
                    )
                    row["person_id"] = person_id
                    row["person_name"] = person_name

                    for raw in normalize_sc13([row]):
                        before = _count_filings(conn)
                        fid = write_filing(conn, raw, clock)
                        after = _count_filings(conn)
                        if fid is None:
                            src.n_skipped += 1
                        elif after > before:
                            src.n_written += 1
                        else:
                            src.n_skipped += 1
                except Exception as exc:
                    msg = f"form13d_activist/{cik}/{accession}: write error: {exc}"
                    log.warning(
                        "run_ingest.form13d_activist_write_error",
                        cik=cik,
                        accession=accession,
                        error=str(exc),
                    )
                    src.errors.append(msg)
                    src.n_skipped += 1

        except Exception as exc:
            msg = f"form13d_activist/{cik}/{accession}: fetch/parse error: {exc}"
            log.warning(
                "run_ingest.form13d_activist_filing_error",
                cik=cik,
                accession=accession,
                error=str(exc),
            )
            src.errors.append(msg)
            src.n_skipped += 1
            # Continue to the next filing — fault-isolated.


# ---------------------------------------------------------------------------
# Private: congress ingestion
# ---------------------------------------------------------------------------

def _ingest_congress(
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    summary: IngestSummary,
    as_of: datetime,
    start_date: str,
    end_date: str,
) -> None:
    """Ingest House + Senate disclosures and write them to the DB."""
    src = SourceSummary()
    summary.per_source["congress"] = src

    client = CongressClient()
    year = as_of.year

    # --- House: official FD.zip index → electronic PTR PDFs → RawFilings ---
    try:
        house_filings = fetch_house_ptrs(client, year, limit=50)
        src.n_fetched += len(house_filings)
        _write_congress_batch(
            house_filings,
            conn=conn,
            clock=clock,
            chamber="house",
            src=src,
        )
    except Exception as exc:
        msg = f"congress/house: fetch error: {exc}"
        log.warning("run_ingest.congress_house_error", error=str(exc))
        src.errors.append(msg)

    # --- Senate: efdsearch CSRF flow ---
    try:
        senate_filings = fetch_senate_ptrs(client, year, limit=50)
        src.n_fetched += len(senate_filings)
        _write_congress_batch(
            senate_filings,
            conn=conn,
            clock=clock,
            chamber="senate",
            src=src,
        )
    except Exception as exc:
        msg = f"congress/senate: fetch error: {exc}"
        log.warning("run_ingest.congress_senate_error", error=str(exc))
        src.errors.append(msg)
        summary.notes.append(f"congress/senate: error — {exc} (House data still written)")


def _write_congress_batch(
    filings: list[dict],
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    chamber: str,
    src: SourceSummary,
) -> None:
    """Write a list of normalised Congress RawFiling dicts to the DB."""
    for raw in filings:
        try:
            # Resolve identity for Congress members.
            person_id = resolve_person(
                raw["person_name"],
                "congress",
                None,
                conn,
                clock,
            )
            raw = dict(raw)
            raw["person_id"] = person_id

            # Congress filings have no accession number; use person+ticker+date
            # as a de-facto key.  The writer will deduplicate on accession if
            # present; for None it falls through to a fresh insert each run.
            # We generate a stable accession substitute so re-runs are idempotent.
            if not raw.get("accession"):
                raw["accession"] = _congress_accession(raw)

            before = _count_filings(conn)
            fid = write_filing(conn, raw, clock)
            after = _count_filings(conn)
            if fid is None:
                # 10b5-1 exclusion — writer returned None.
                src.n_skipped += 1
            elif after > before:
                # A new row was actually inserted.
                src.n_written += 1
            else:
                # Duplicate — writer returned existing id, no new row.
                src.n_skipped += 1
        except Exception as exc:
            msg = f"congress/{chamber}/{raw.get('ticker','?')}: write error: {exc}"
            log.warning(
                "run_ingest.congress_write_error",
                chamber=chamber,
                ticker=raw.get("ticker"),
                error=str(exc),
            )
            src.errors.append(msg)
            src.n_skipped += 1


def _count_filings(conn: sqlite3.Connection) -> int:
    """Return the total number of rows in the filings table (all states)."""
    return conn.execute("SELECT count(*) FROM filings").fetchone()[0]


def _congress_accession(raw: dict) -> str:
    """Derive a stable idempotency key for a Congress filing that has no accession.

    Uses the person_id + ticker + filing_ts combination so re-running the
    ingest with the same data does not create duplicate rows.  The writer's
    accession-level dedup then treats this as the canonical key.
    """
    key = f"congress:{raw.get('person_id','')}:{raw.get('ticker','')}:{raw.get('filing_ts','')}:{raw.get('txn_idx','')}"
    return "CONG-" + hashlib.sha256(key.encode()).hexdigest()[:24].upper()


# ---------------------------------------------------------------------------
# Internal clock helpers (no datetime.now)
# ---------------------------------------------------------------------------

def _parse_clock_to_datetime(clock_str: str) -> datetime:
    """Parse a clock() ISO-8601 string to a tz-aware datetime.

    Falls back to a UTC epoch value if parsing fails so the runner degrades
    gracefully rather than crashing on a bad clock string.
    """
    try:
        dt = datetime.fromisoformat(clock_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        log.warning("run_ingest.bad_clock_str", clock_str=clock_str)
        return datetime(2000, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Module-level helpers for form13f (monkeypatchable in tests)
# ---------------------------------------------------------------------------

def _make_edgar_for_form13f(config: "Config") -> "EdgarClient":
    """Return a fresh EdgarClient for form13f ingestion.

    Factored out so tests can monkeypatch this by name.
    """
    return EdgarClient(config=config)


def _alpaca_asset_lookup(config: "Config") -> "Callable[[], dict[str, str]]":
    """Return a callable that yields {ISSUER_NAME_UPPER: ticker} for tradeable US equities.

    Calls Alpaca's GET /v2/assets?asset_class=us_equity&status=active once and
    caches the result.  On ANY failure (missing keys, network error, wrong
    backend) returns lambda: {} so CUSIP resolution falls back to the
    cusip_map cache + seed only (never crashes, never blocks ingest).
    """
    _cache: dict[str, str] | None = None

    def _fetch() -> dict[str, str]:
        nonlocal _cache
        if _cache is not None:
            return _cache

        try:
            # Only attempt live lookup when running against Alpaca paper backend.
            if config.executor_backend != "alpaca_paper":
                _cache = {}
                return _cache
            if not config.alpaca_api_key or not config.alpaca_secret_key:
                _cache = {}
                return _cache

            from arbiter.execution.alpaca_adapter import AlpacaAdapter, _default_http_get

            adapter = AlpacaAdapter(config=config)
            url = (
                f"{adapter._base()}/v2/assets"
                "?asset_class=us_equity&status=active&tradable=true"
            )
            data = _default_http_get(url, adapter._headers())

            lookup: dict[str, str] = {}
            for asset in data:
                name = asset.get("name") or ""
                symbol = asset.get("symbol") or ""
                if name and symbol:
                    lookup[name.upper()] = symbol

            log.info(
                "form13f.alpaca_asset_lookup.loaded",
                count=len(lookup),
            )
            _cache = lookup
            return _cache

        except Exception as exc:
            log.warning(
                "form13f.alpaca_asset_lookup.failed",
                error=str(exc),
            )
            _cache = {}
            return _cache

    return _fetch


# ---------------------------------------------------------------------------
# Private: form13f ingestion
# ---------------------------------------------------------------------------

def _normalize_filing_date(filed_at: str) -> str:
    """Normalize a filed_at string to a tz-aware ISO string.

    EDGAR refs carry ``filed_at`` as bare dates like ``"2026-05-15"``.
    Append ``T00:00:00+00:00`` when it looks like a bare date so that
    detection's ``_parse_ts`` and PIT comparisons work consistently.
    """
    if filed_at and len(filed_at) == 10 and "T" not in filed_at:
        return filed_at + "T00:00:00+00:00"
    return filed_at


def _ingest_form13f(
    config: "Config",
    *,
    conn: sqlite3.Connection,
    clock: Callable[[], str],
    summary: "IngestSummary",
) -> None:
    """Ingest 13F-HR holdings for each tracked fund manager.

    Mirrors ``_ingest_sc13`` in structure:
    - UA-empty guard → warn + return (not a crash; other sources still run).
    - Registers each manager as a ``people`` row with ``source="form13f"``.
    - Fetches each manager's recent 13F-HR refs; dedupes to latest accession
      per report_date; processes the 1–2 most recent report_dates.
    - Stores holdings, computes deltas, writes each delta as a filings row.
    """
    from arbiter.data.fund_managers import FUND_MANAGERS, manager_ciks as _manager_ciks
    from arbiter.ingest.edgar.form13f_normalize import store_holdings, compute_deltas
    from arbiter.ingest.edgar.form13f_parser import parse_form13f_infotable

    src = SourceSummary()
    summary.per_source["form13f"] = src

    # Guard: edgar_user_agent required.  Skip form13f (not a crash) if absent.
    if not config.edgar_user_agent or not config.edgar_user_agent.strip():
        msg = (
            "form13f skipped: Config.edgar_user_agent is empty. "
            "Set EDGAR_USER_AGENT or [edgar] user_agent in arbiter.toml."
        )
        log.warning("run_ingest.form13f_skipped_no_user_agent")
        summary.notes.append(msg)
        src.errors.append(msg)
        return

    # Determine the CIK set (env override or full roster).
    cik_set: tuple[str, ...] = config.form13f_manager_ciks or _manager_ciks()

    # Build {cik: person_id} by registering each manager in people.
    cik_to_person: dict[str, str] = {}
    for m in FUND_MANAGERS:
        if m.cik not in cik_set:
            continue
        try:
            person_id = resolve_person(m.name, "form13f", {}, conn, clock)
            cik_to_person[m.cik] = person_id
        except Exception as exc:
            msg = f"form13f/people: resolve_person failed for {m.name}: {exc}"
            log.warning("run_ingest.form13f_resolve_person_error", name=m.name, error=str(exc))
            src.errors.append(msg)

    # Prepare EDGAR client and Alpaca asset lookup.
    asset_lookup = _alpaca_asset_lookup(config)
    client = _make_edgar_for_form13f(config)

    try:
        for cik, person_id in cik_to_person.items():
            try:
                refs = client.search_form13f_filings(cik)
            except Exception as exc:
                msg = f"form13f/{cik}: search failed: {exc}"
                log.warning("run_ingest.form13f_search_error", cik=cik, error=str(exc))
                src.errors.append(msg)
                continue

            if not refs:
                continue

            # --- Amendment dedupe: keep only the latest-filed accession per report_date ---
            # Group refs by report_date, select the one with the most recent filed_at.
            by_report: dict[str, dict] = {}
            for ref in refs:
                rd = ref.get("report_date", "")
                existing = by_report.get(rd)
                if existing is None or ref.get("filed_at", "") > existing.get("filed_at", ""):
                    by_report[rd] = ref

            # Process the 2 most recent quarters: store BOTH (oldest first as a
            # baseline) but emit delta signals ONLY for the NEWEST quarter, so
            # the newest diffs against the older baseline (a real quarter-over-
            # quarter delta) instead of BOTH being treated as first-filing
            # snapshots.  A manager with only one available quarter yields a
            # first-filing top-K conviction snapshot (the intended cold-start).
            selected = sorted(by_report.keys(), reverse=True)[:2]
            newest_rd = selected[0] if selected else None

            for rd in sorted(selected):  # oldest → newest (baseline before delta)
                ref = by_report[rd]
                accession = ref.get("accession", "")
                filed_at_raw = ref.get("filed_at", "")
                report_date = ref.get("report_date", "")

                if not accession or not report_date:
                    src.n_skipped += 1
                    continue

                # Normalize bare date → tz-aware ISO string.
                filing_date = _normalize_filing_date(filed_at_raw)

                src.n_fetched += 1
                try:
                    xml = client.get_form13f_info_table(accession, cik)
                    if not xml:
                        src.n_skipped += 1
                        continue

                    holdings = parse_form13f_infotable(xml)
                    store_holdings(
                        conn,
                        person_id,
                        accession,
                        filing_date,
                        report_date,
                        holdings,
                        asset_lookup=asset_lookup,
                        now_iso=clock(),
                    )

                    # Emit signals only for the NEWEST quarter (deltas vs the
                    # older baseline stored just above).
                    if rd != newest_rd:
                        continue

                    deltas = compute_deltas(conn, person_id, report_date, config=config)
                    for delta in deltas:
                        d = dict(delta)
                        d["person_id"] = person_id
                        try:
                            before = _count_filings(conn)
                            fid = write_filing(conn, d, clock)
                            after = _count_filings(conn)
                            if fid is None:
                                src.n_skipped += 1
                            elif after > before:
                                src.n_written += 1
                            else:
                                src.n_skipped += 1
                        except Exception as exc:
                            msg = (
                                f"form13f/{cik}/{accession}: write_filing error: {exc}"
                            )
                            log.warning(
                                "run_ingest.form13f_write_error",
                                cik=cik,
                                accession=accession,
                                error=str(exc),
                            )
                            src.errors.append(msg)
                            src.n_skipped += 1

                except Exception as exc:
                    msg = f"form13f/{cik}/{accession}: fetch/parse error: {exc}"
                    log.warning(
                        "run_ingest.form13f_filing_error",
                        cik=cik,
                        accession=accession,
                        error=str(exc),
                    )
                    src.errors.append(msg)
                    src.n_skipped += 1
                    # Continue to the next report_date — fault-isolated.

    finally:
        try:
            client.close()
        except Exception:
            pass
