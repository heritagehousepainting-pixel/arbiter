"""Tests for filer-CIK 13D ingestion (named-activist discovery path).

Covers ``runner._ingest_sc13_by_filer`` + ``_resolve_subject_ticker``:
- happy path: a structured 13D discovered by filer CIK → subject ticker
  resolved → ``source='form13d'`` filing written;
- subject resolution priority (subject-CIK exact, then CUSIP), and the
  safety-first DROP when neither resolves;
- UA-empty guard skips the path (no crash);
- ``form13d`` remains in run_ingest's default sources.
"""
from __future__ import annotations

import inspect

NOW_ISO = "2026-06-24T00:00:00+00:00"


def _structured_13d(*, issuer_cik: str = "0000012345", cusip: str = "ZZZ999999") -> str:
    return (
        "<edgarSubmission><documentType>SC 13D</documentType>"
        "<rptOwnerCik>0001517137</rptOwnerCik>"
        "<rptOwnerName>Starboard Value LP</rptOwnerName>"
        "<issuerName>Acme Corp</issuerName>"
        f"<issuerCik>{issuer_cik}</issuerCik>"
        f"<cusip>{cusip}</cusip>"
        "<percentOfClass>7.5</percentOfClass>"
        "<aggregateAmountOwned>1000000</aggregateAmountOwned>"
        "<dateOfEvent>2026-06-20</dateOfEvent>"
        "<filingDate>2026-06-22</filingDate></edgarSubmission>"
    )


class _FakeEdgar:
    """Fake EdgarClient for the activist filer path.

    Returns ONE 13D ref per filer CIK; serves a structured doc; resolves the
    subject issuer CIK 0000012345 -> ACME.  ``calls`` records search CIKs.
    """

    def __init__(self, *, doc: str | None = None, cik_ticker: dict | None = None):
        self._doc = doc if doc is not None else _structured_13d()
        self._cik_ticker = cik_ticker if cik_ticker is not None else {"0000012345": "ACME"}
        self.searched: list[str] = []

    def search_sc13_by_filer(self, cik, *, count=20):
        self.searched.append(cik)
        return [
            {
                "cik": cik,
                "accession": f"acc-{cik}",
                "filed_at": "2026-06-22",
                "primary_document": "p.xml",
                "schedule": "13D",
                "is_amendment": False,
            }
        ]

    def get_sc13_doc(self, accession, cik, *, primary_document=None):
        return self._doc

    def get_ticker_for_cik(self, cik):
        return self._cik_ticker.get(str(cik).strip())

    def close(self):
        pass


def _make_migrated_conn(tmp_path):
    from arbiter.db.connection import get_connection
    from arbiter.db.migrate import run_migrations

    conn = get_connection(str(tmp_path / "t.db"))
    run_migrations(conn)
    return conn


def _make_config(*, edgar_user_agent: str = "TestBot test@example.com"):
    from arbiter.config import Config

    return Config(
        live_trading=False,
        executor_backend="sim",
        db_path=":memory:",
        audit_path="/tmp/test_audit.jsonl",
        metrics_path="/tmp/test_metrics.jsonl",
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent=edgar_user_agent,
        kill_switch_url="",
        alert_webhook_url="",
    )


def test_filer_path_resolves_subject_cik_and_writes_filing(monkeypatch, tmp_path):
    """Structured 13D discovered by filer CIK → subject CIK→ticker → filing."""
    from arbiter.data.activist_filers import ACTIVIST_FILERS
    from arbiter.ingest import runner

    fake = _FakeEdgar()
    monkeypatch.setattr(runner, "EdgarClient", lambda config: fake)
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {}))

    conn = _make_migrated_conn(tmp_path)
    cfg = _make_config()
    summary = runner.IngestSummary(sources=("form13d",))

    runner._ingest_sc13_by_filer(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    # Every roster filer was searched by its own CIK.
    assert set(fake.searched) == {a.cik for a in ACTIVIST_FILERS}

    rows = conn.execute(
        "SELECT ticker, source, person_id FROM filings WHERE source='form13d'"
    ).fetchall()
    assert len(rows) >= 1
    assert all(r["ticker"] == "ACME" for r in rows)

    # Counts landed in the dedicated activist bucket.
    src = summary.per_source["form13d_activist"]
    assert src.n_written >= 1


def test_filer_path_drops_when_subject_unresolvable(monkeypatch, tmp_path):
    """No subject CIK match and an unknown CUSIP → DROP (never trade a guess)."""
    from arbiter.ingest import runner

    # Doc whose issuer CIK is NOT in the reverse map and CUSIP is unknown.
    fake = _FakeEdgar(
        doc=_structured_13d(issuer_cik="0009999999", cusip="UNKNOWN00"),
        cik_ticker={},  # nothing resolves
    )
    monkeypatch.setattr(runner, "EdgarClient", lambda config: fake)
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {}))

    conn = _make_migrated_conn(tmp_path)
    cfg = _make_config()
    summary = runner.IngestSummary(sources=("form13d",))

    runner._ingest_sc13_by_filer(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    n = conn.execute(
        "SELECT COUNT(*) FROM filings WHERE source='form13d'"
    ).fetchone()[0]
    assert n == 0
    assert summary.per_source["form13d_activist"].n_skipped >= 1


def test_filer_path_resolves_via_cusip_when_no_subject_cik(monkeypatch, tmp_path):
    """No subject CIK, but CUSIP resolves via the seed map → ticker resolved."""
    from arbiter.ingest import runner

    # NVDA seed CUSIP (67066G104) is in cusip_resolver._SEED; no issuer CIK.
    fake = _FakeEdgar(
        doc=_structured_13d(issuer_cik="", cusip="67066G104"),
        cik_ticker={},
    )
    monkeypatch.setattr(runner, "EdgarClient", lambda config: fake)
    monkeypatch.setattr(runner, "_alpaca_asset_lookup", lambda cfg: (lambda: {}))

    conn = _make_migrated_conn(tmp_path)
    cfg = _make_config()
    summary = runner.IngestSummary(sources=("form13d",))

    runner._ingest_sc13_by_filer(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    rows = conn.execute(
        "SELECT ticker FROM filings WHERE source='form13d'"
    ).fetchall()
    assert any(r["ticker"] == "NVDA" for r in rows)


def test_filer_path_ua_empty_guard(monkeypatch, tmp_path):
    """Empty edgar_user_agent → activist filer path skipped, no EdgarClient."""
    from arbiter.ingest import runner

    created = []
    monkeypatch.setattr(
        runner, "EdgarClient", lambda config: created.append(1) or _FakeEdgar()
    )

    conn = _make_migrated_conn(tmp_path)
    cfg = _make_config(edgar_user_agent="")
    summary = runner.IngestSummary(sources=("form13d",))

    runner._ingest_sc13_by_filer(cfg, conn=conn, clock=lambda: NOW_ISO, summary=summary)

    assert created == [], "EdgarClient must not be built when UA is empty"
    n = conn.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
    assert n == 0


def test_form13d_in_default_sources():
    from arbiter.ingest.runner import run_ingest

    sig = inspect.signature(run_ingest)
    assert "form13d" in sig.parameters["sources"].default
