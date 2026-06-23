"""Assemble an EvidencePack and fill its source_fingerprint.

Pure. Combines the technical + (optional) fundamental features for a ticker as
of a timestamp and computes the dedup/audit fingerprint via
types.compute_fingerprint.

ISOLATION: pure stdlib + mirofish.types. Never imports arbiter.
"""
from __future__ import annotations

from datetime import datetime

from mirofish.types import (
    EvidencePack,
    FundamentalFeatures,
    TechnicalFeatures,
    compute_fingerprint,
    ensure_utc,
)


def build_pack(
    ticker: str,
    as_of: datetime,
    tech: TechnicalFeatures,
    fund: FundamentalFeatures | None,
) -> EvidencePack:
    """Assemble a frozen EvidencePack with source_fingerprint filled. Pure."""
    fingerprint = compute_fingerprint(ticker, tech, fund)
    return EvidencePack(
        ticker=ticker,
        as_of=ensure_utc(as_of),
        technical=tech,
        fundamental=fund,
        source_fingerprint=fingerprint,
    )
