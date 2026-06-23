"""Tests for build_pack + fingerprint stability."""
from __future__ import annotations

import re
from datetime import datetime, timezone

from mirofish.evidence.pack import build_pack
from mirofish.types import FundamentalFeatures, TechnicalFeatures

_AS_OF = datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
_HEX16 = re.compile(r"^[0-9a-f]{16}$")


def _tech(last_close: float = 100.0) -> TechnicalFeatures:
    return TechnicalFeatures(
        last_close=last_close, ma_50=95.0, ma_200=90.0,
        pct_vs_ma_50=0.05, pct_vs_ma_200=0.11, momentum_20d=0.08,
        rsi_14=70.0, realized_vol_annualized=0.3, pct_from_52w_high=-0.02,
        pct_from_52w_low=0.5, volume_surge_ratio=1.5, n_bars=220,
    )


def _fund() -> FundamentalFeatures:
    return FundamentalFeatures(
        revenue_ttm=1000.0, revenue_growth_yoy=0.2, gross_margin=0.5,
        operating_margin=0.25, net_income_ttm=100.0, shares_outstanding=40.0,
        pe_ratio=40.0, ps_ratio=4.0, sector="Technology", valuation_z=1.0,
        as_of_latest_filed="2026-04-20",
    )


def test_fingerprint_is_16_hex():
    pack = build_pack("AAPL", _AS_OF, _tech(), _fund())
    assert _HEX16.match(pack.source_fingerprint)


def test_fingerprint_stable_across_identical_builds():
    p1 = build_pack("AAPL", _AS_OF, _tech(), _fund())
    # Different as_of time-of-day must NOT change the fingerprint.
    other_as_of = datetime(2026, 6, 1, 18, 30, 0, tzinfo=timezone.utc)
    p2 = build_pack("AAPL", other_as_of, _tech(), _fund())
    assert p1.source_fingerprint == p2.source_fingerprint


def test_fingerprint_changes_when_feature_changes():
    p1 = build_pack("AAPL", _AS_OF, _tech(100.0), _fund())
    p2 = build_pack("AAPL", _AS_OF, _tech(101.0), _fund())
    assert p1.source_fingerprint != p2.source_fingerprint


def test_fingerprint_casing_insensitive_ticker():
    p1 = build_pack("AAPL", _AS_OF, _tech(), _fund())
    p2 = build_pack("aapl", _AS_OF, _tech(), _fund())
    assert p1.source_fingerprint == p2.source_fingerprint


def test_none_fundamental_fingerprints_without_error():
    pack = build_pack("AAPL", _AS_OF, _tech(), None)
    assert _HEX16.match(pack.source_fingerprint)
    assert pack.fundamental is None


def test_pack_carries_fields_and_normalizes_as_of():
    naive = datetime(2026, 6, 1, 0, 0, 0)  # no tzinfo
    pack = build_pack("AAPL", naive, _tech(), _fund())
    assert pack.ticker == "AAPL"
    assert pack.as_of.tzinfo is not None  # normalized to tz-aware UTC
    assert pack.technical.last_close == 100.0
    assert pack.fundamental is not None
