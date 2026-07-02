"""Tier-3 #12 — catalyst-gated A3 news sweep (2026-07-02).

The engine's A3 gather now sweeps ONLY catalyst tickers (held / fresh signal /
active idea) instead of the full 138-name watchlist that took 30+ min per full
cycle under Finnhub free-tier rate limits.
"""
from __future__ import annotations

from types import SimpleNamespace

from arbiter.engine._engine import Engine


def _stub():
    return SimpleNamespace(conn=None, clock=None, config=SimpleNamespace())


def test_explicit_tickers_pass_through(monkeypatch):
    captured: dict = {}

    def fake(conn, clock, config, watchlist):
        captured["watchlist"] = watchlist
        return []

    import arbiter.adapters.a3 as a3

    monkeypatch.setattr(a3, "gather_a3_opinions", fake)
    Engine._gather_a3_opinions(_stub(), ["BAC", "LULU"])
    assert captured["watchlist"] == ["BAC", "LULU"]


def test_empty_catalyst_set_skips_adapter_entirely(monkeypatch):
    called: list = []

    import arbiter.adapters.a3 as a3

    monkeypatch.setattr(a3, "gather_a3_opinions", lambda *a: called.append(1) or [])
    assert Engine._gather_a3_opinions(_stub(), []) == []
    assert called == []  # zero catalysts → zero Finnhub calls


def test_none_falls_back_to_full_watchlist(monkeypatch):
    captured: dict = {}

    def fake(conn, clock, config, watchlist):
        captured["n"] = len(watchlist)
        return []

    import arbiter.adapters.a3 as a3

    monkeypatch.setattr(a3, "gather_a3_opinions", fake)
    Engine._gather_a3_opinions(_stub(), None)
    assert captured["n"] > 100  # legacy full-watchlist behavior preserved
