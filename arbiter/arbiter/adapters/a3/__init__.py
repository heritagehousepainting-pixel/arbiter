"""A3 news advisor — free corroborated news pipeline.

The frozen public entry point is ``gather_a3_opinions``.

    from arbiter.adapters.a3 import gather_a3_opinions

Advisor ID: ``"A3.news"``.
Horizon:    7 days (SHORT bucket).
Gate:       ≥2 independent publishers (Finnhub transport, editorial source_id).
Inert:      returns [] when ``config.finnhub_api_key`` is empty.
Network-gated: returns [] under ``BacktestClock``.

NOTE (ToS): Finnhub free tier is for personal / non-commercial use only.
See ``arbiter/arbiter/ingest/finnhub/client.py`` for full ToS notice.
"""
from __future__ import annotations

from arbiter.adapters.a3.pipeline import ADVISOR_ID, gather_a3_opinions

__all__ = ["ADVISOR_ID", "gather_a3_opinions"]
