"""Ingest sub-package — Lane 5 orchestration.

Public entry point:
    run_ingest(config, *, conn, clock, sources, tickers, lookback_days) -> IngestSummary
"""
from __future__ import annotations

from arbiter.ingest.runner import run_ingest, IngestSummary

__all__ = ["run_ingest", "IngestSummary"]
