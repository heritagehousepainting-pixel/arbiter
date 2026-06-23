# mirofish/types.py
"""Frozen shared contracts for the MiroFish A2 brain service.

This module is the single source of truth for every type that crosses a seam
between the evidence layer, the judge, and the FastAPI service. It is created
in the foundation step and is NOT owned/edited by any build lane.

ISOLATION: this module (and the whole `mirofish` package) must never
`import arbiter`. These dataclasses are vendored copies, intentionally
independent of arbiter's own types.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- #
# Horizon constants (design spec §2.3, §3.4). SHORT = technical-led opinion,
# MEDIUM = fundamental-led opinion. Both satisfy the client's 0 < h <= 365.
# --------------------------------------------------------------------------- #
SHORT_DAYS: int = 10
MEDIUM_DAYS: int = 60

# Clamp ranges the judge re-applies in Python (JSON-Schema bounds are HINTS).
STANCE_MIN: float = -1.0
STANCE_MAX: float = 1.0
CONFIDENCE_MIN: float = 1e-6   # strictly > 0 (client gate is (0, 1])
CONFIDENCE_MAX: float = 1.0


def ensure_utc(dt: datetime) -> datetime:
    """Normalize a datetime to tz-aware UTC. Naive -> assume UTC; aware -> convert."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# --------------------------------------------------------------------------- #
# Market data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Bar:
    """One daily OHLCV bar. Vendored — NOT arbiter.data.pit.Bar.

    `t` is a tz-aware UTC datetime (the bar's timestamp). Field names o/h/l/c/v
    mirror Alpaca's wire keys for clarity at the parse seam.
    """
    t: datetime          # tz-aware UTC
    o: float             # open
    h: float             # high
    l: float             # low   # noqa: E741 — matches Alpaca wire key
    c: float             # close
    v: float             # volume (shares)


# --------------------------------------------------------------------------- #
# Evidence features (all fields are plain floats/ints/None — JSON-serializable,
# so they fold cleanly into the source_fingerprint canonical JSON).
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class TechnicalFeatures:
    """Deterministic price-action features computed from bars <= as_of.

    Units are documented per-field. `None` where insufficient history exists
    (e.g. ma_200 needs >=200 bars); the judge prompt renders None as "n/a".
    """
    last_close: float                 # USD, most recent close <= as_of
    ma_50: float | None               # USD, 50-day simple MA of closes
    ma_200: float | None              # USD, 200-day simple MA of closes
    pct_vs_ma_50: float | None        # fraction, (last_close/ma_50 - 1); +0.05 = 5% above
    pct_vs_ma_200: float | None       # fraction
    momentum_20d: float | None        # fraction, 20-trading-day return (close_t/close_t-20 - 1)
    rsi_14: float | None              # 0..100, Wilder RSI over 14 periods
    realized_vol_annualized: float | None  # fraction, stdev(daily log-returns, 20d) * sqrt(252)
    pct_from_52w_high: float | None   # fraction <= 0, (last_close/52w_high - 1); -0.20 = 20% below high
    pct_from_52w_low: float | None    # fraction >= 0, (last_close/52w_low - 1)
    volume_surge_ratio: float | None  # ratio, last_volume / avg(volume, trailing 20d); 2.0 = double
    n_bars: int                       # number of eligible bars used (audit)


@dataclass(frozen=True)
class FundamentalFeatures:
    """Point-in-time fundamentals from SEC companyfacts (facts with filed <= as_of).

    Ratios (pe, ps) are derived with the Alpaca last_close price * shares.
    `valuation_z` is this name's P/E vs its sector peers (z-score); positive =
    richer than sector (a bearish-leaning input). All None-able where the tag /
    peer set is unavailable.
    """
    revenue_ttm: float | None             # USD, trailing reported revenue (latest filed)
    revenue_growth_yoy: float | None      # fraction, (rev_latest/rev_year_ago - 1)
    gross_margin: float | None            # fraction, gross_profit / revenue
    operating_margin: float | None        # fraction, operating_income / revenue
    net_income_ttm: float | None          # USD
    shares_outstanding: float | None      # shares (dei / weighted diluted)
    pe_ratio: float | None                # price*shares / net_income (None if earnings<=0)
    ps_ratio: float | None                # price*shares / revenue
    sector: str | None                    # vendored sector label, or None
    valuation_z: float | None             # z-score of pe_ratio vs sector peers (+ = richer)
    as_of_latest_filed: str | None        # ISO date of the newest fact used (audit; <= as_of)


# --------------------------------------------------------------------------- #
# Evidence pack + fingerprint
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class EvidencePack:
    """Everything the judge sees. `source_fingerprint` is the audit/dedup key.

    The fingerprint is computed over technical + fundamental only (NOT over
    as_of's time-of-day, NOT over the ticker casing) so identical evidence on
    the same logical name dedups. ticker/as_of are carried for prompt context.
    """
    ticker: str
    as_of: datetime                       # tz-aware UTC (the information timestamp)
    technical: TechnicalFeatures
    fundamental: FundamentalFeatures | None
    source_fingerprint: str = field(default="")  # filled by build_pack via compute_fingerprint


def _canonical_json(obj: object) -> str:
    """Deterministic JSON: sorted keys, no whitespace, ASCII, stable floats."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def compute_fingerprint(ticker: str, technical: TechnicalFeatures,
                        fundamental: FundamentalFeatures | None) -> str:
    """sha256(canonical_json(evidence))[:16]. Stable across runs/processes.

    Excludes as_of so the same evidence on the same day dedups; the service
    cache key separately carries as_of.date(). ticker is upper-cased for
    stability. Returns a 16-char lowercase hex string.
    """
    payload = {
        "ticker": ticker.upper(),
        "technical": asdict(technical),
        "fundamental": asdict(fundamental) if fundamental is not None else None,
    }
    digest = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    return digest[:16]


@dataclass(frozen=True)
class OpinionOut:
    """One opinion as it appears in the /analyze response `opinions` array.

    Field names + value ranges match the arbiter client's expectations exactly
    (adapter._opinions_from_response). stance_score may be negative.
    """
    stance_score: float          # [-1, 1]; negative = bearish (passthrough)
    confidence: float            # (0, 1]
    horizon_days: int            # SHORT_DAYS or MEDIUM_DAYS (0 < h <= 365)
    rationale: str               # grounded in evidence, <= 600 chars
    source_fingerprint: str      # the pack's fingerprint (NOT the idea fingerprint)


# --------------------------------------------------------------------------- #
# Pydantic request / response models for FastAPI (/analyze).
# These match the arbiter client wire contract byte-for-byte.
# --------------------------------------------------------------------------- #
class AnalyzeRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)
    as_of: datetime                      # pydantic parses ISO-8601; must be tz-aware UTC
    idea_fingerprint: str = Field(default="", max_length=128)


class OpinionModel(BaseModel):
    stance_score: float
    confidence: float
    horizon_days: int
    rationale: str
    source_fingerprint: str


class AnalyzeResponse(BaseModel):
    opinions: list[OpinionModel]
    run_id: str


def opinion_to_model(op: OpinionOut) -> OpinionModel:
    return OpinionModel(
        stance_score=op.stance_score,
        confidence=op.confidence,
        horizon_days=op.horizon_days,
        rationale=op.rationale,
        source_fingerprint=op.source_fingerprint,
    )
