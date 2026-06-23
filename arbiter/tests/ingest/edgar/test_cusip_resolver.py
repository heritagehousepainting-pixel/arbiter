# tests/ingest/edgar/test_cusip_resolver.py
import sqlite3
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.ingest.edgar import cusip_resolver as cr

def _conn():
    c = get_connection(":memory:")
    run_migrations(c)
    return c

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
