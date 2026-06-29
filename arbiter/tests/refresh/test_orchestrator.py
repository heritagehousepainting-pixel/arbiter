# tests/refresh/test_orchestrator.py
import sqlite3
from datetime import datetime, timezone
from types import SimpleNamespace

from arbiter.refresh.orchestrator import run_monday_refresh
from arbiter.refresh.findings_store import create_table
from arbiter.refresh.llm import FakeLLM


class _Exec:
    def get_positions(self): return {"UBER": object()}


class _Clock:
    def now(self): return datetime(2026, 6, 29, tzinfo=timezone.utc)


class _FakeFinnhub:
    def get_company_news(self, t, frm, to): return [{"headline": "h"}]
    def get_news_sentiment(self, t): return {"sentiment_score": -0.6}


CANNED = ('```json\n{"market": [{"summary": "CPI", "severity": "high", '
          '"affected_tickers": ["UBER"], "sources": []}], "stale_sources": []}\n```')


def test_orchestrator_runs_and_feeds(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(":memory:")
    create_table(conn)
    engine = SimpleNamespace(
        conn=conn, clock=_Clock(), executor=_Exec(),
        config=SimpleNamespace(anthropic_api_key="k", refresh_model="claude-opus-4-8",
                               a4_advisor_id="A4.macro", a4_min_confidence=0.0,
                               edgar_user_agent="", audit_path=str(tmp_path/"a.jsonl")))
    ingested = []
    report = run_monday_refresh(
        engine, llm=FakeLLM(CANNED), finnhub=_FakeFinnhub(),
        ingest_fn=lambda **kw: ingested.append(kw.get("sources")),
        alerting=SimpleNamespace(notify=lambda *a, **k: None))
    assert report.positions[0].ticker == "UBER"
    assert "UBER" in report.fed_tickers
    assert (tmp_path / "data" / "monday-refresh-2026-06-29.md").exists()


def test_one_scan_failure_does_not_abort(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    conn = sqlite3.connect(":memory:")
    create_table(conn)
    class _BadFinnhub:
        def get_company_news(self, *a): raise RuntimeError("x")
        def get_news_sentiment(self, *a): raise RuntimeError("x")
    engine = SimpleNamespace(
        conn=conn, clock=_Clock(), executor=_Exec(),
        config=SimpleNamespace(anthropic_api_key="", refresh_model="claude-opus-4-8",
                               a4_advisor_id="A4.macro", a4_min_confidence=0.0,
                               edgar_user_agent="", audit_path=str(tmp_path/"a.jsonl")))
    report = run_monday_refresh(
        engine, llm=None, finnhub=_BadFinnhub(),
        ingest_fn=lambda **kw: None,
        alerting=SimpleNamespace(notify=lambda *a, **k: None))
    assert report.positions[0].available is False   # finnhub failed, still reported
    assert report.macro.available is False           # no key, skipped
    assert (tmp_path / "data" / "monday-refresh-2026-06-29.md").exists()
