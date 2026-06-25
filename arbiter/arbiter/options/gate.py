"""Options expression gate — evaluates whether a thesis qualifies for options expression.

The gate answers a single question: "Is this thesis worth expressing as a
long-dated long call/put?"  It applies ALL of the following criteria in order
(first failure short-circuits and sets ``reason``):

    1. OPTIONS_ON   : config.options_mode != "off"
    2. CONVICTION   : |conviction| >= equity_entry_threshold × option_conviction_mult
    3. HORIZON      : horizon_days >= option_min_expiry_days
    4. CATALYST     : catalyst_tag is present and non-empty
    5. IV_CHEAP     : if iv_rank() is not None → ivr <= option_ivr_max
                      if iv_rank() is None (cold-start) → use realized_vol_proxy
                      as a sanity bound (does NOT hard-block when proxy is also None)

When ALL pass, returns ``OptionGateDecision(express=True, reason="OK", ...)``.
On first failure, returns ``OptionGateDecision(express=False, reason=<CODE>, ...)``.

Reason codes (fixed vocabulary for SQL aggregation)
----------------------------------------------------
    "OK"                 — gate passed
    "OPTIONS_OFF"        — config.options_mode == "off"
    "CONVICTION_TOO_LOW" — |conviction| below threshold
    "HORIZON_TOO_SHORT"  — horizon_days < option_min_expiry_days
    "NO_CATALYST"        — no recognised catalyst tag present
    "IV_RANK_TOO_HIGH"   — ivr_estimate > option_ivr_max

Cold-start IV rule (P1)
-----------------------
When ``iv_rank()`` returns None (insufficient history), we fall back to
``realized_vol_proxy()``.  The proxy is recorded in the decision for
calibration, but we do NOT hard-block on a missing proxy — we log
``ivr_estimate=None`` and ``realized_vol_proxy=<value or None>`` and let
the thesis proceed.  This avoids starving the shadow log of data when the
IV-history table is freshly populated.

Note: Premium/liquidity checks happen downstream in ``contract_selector`` and
the caller is responsible for logging ``NO_CONTRACT_FOUND`` if ``select_contract``
returns None.
"""
from __future__ import annotations

import logging
import sqlite3

from arbiter.config import Config
from arbiter.options import iv_history
from arbiter.options.alpaca_options_client import AlpacaOptionsClient
from arbiter.options.types import OptionGateDecision, OptionSide

log = logging.getLogger(__name__)


def options_expression_gate(
    conn: sqlite3.Connection,
    client: AlpacaOptionsClient,
    *,
    underlying: str,
    conviction: float,
    horizon_days: float,
    catalyst_tag: str | None,
    equity_entry_threshold: float,
    underlying_price: float,
    config: Config,
    as_of: str,
) -> OptionGateDecision:
    """Evaluate whether a thesis qualifies for options expression.

    Parameters
    ----------
    conn : sqlite3.Connection
        Open arbiter DB connection (used for IV-history lookups).
    client : AlpacaOptionsClient
        Initialised options data client (unused in gate itself but reserved for
        potential IV snapshot fetch by callers sharing the signature).
    underlying : str
        Equity ticker.
    conviction : float
        Fused conviction score (positive = bullish, negative = bearish).
    horizon_days : float
        Thesis horizon in calendar days.
    catalyst_tag : str | None
        Catalyst tag from idea/fusion metadata, e.g. ``"13D"``,
        ``"form4_cluster"``, ``"fund_buy"``.  None → gate rejects.
    equity_entry_threshold : float
        The conviction threshold used by the equity gate.
    underlying_price : float
        Current price of the underlying (recorded in decision; not used in P1
        gate logic but available for future breakeven gate extension).
    config : Config
        Frozen arbiter config.
    as_of : str
        Tz-aware UTC ISO timestamp from the engine clock.

    Returns
    -------
    OptionGateDecision
        ``express=True`` when ALL gate criteria pass; ``express=False`` with
        a reason code otherwise.
    """
    # Shared partial kwargs for every return path — avoids repetitive kwarg lists.
    conviction_threshold_used = equity_entry_threshold * config.option_conviction_mult

    def _reject(reason: str, *, ivr: float | None = None, rvp: float | None = None) -> OptionGateDecision:
        return OptionGateDecision(
            express=False,
            reason=reason,
            side=None,
            target_delta_low=config.option_target_delta_low,
            target_delta_high=config.option_target_delta_high,
            min_expiry_days=config.option_min_expiry_days,
            catalyst_tag=catalyst_tag,
            conviction=conviction,
            conviction_threshold_used=conviction_threshold_used,
            horizon_days=horizon_days,
            ivr_estimate=ivr,
            realized_vol_proxy=rvp,
        )

    # ------------------------------------------------------------------
    # 1. OPTIONS_OFF — entire layer is a no-op when mode == "off"
    # ------------------------------------------------------------------
    if config.options_mode == "off":
        return _reject("OPTIONS_OFF")

    # ------------------------------------------------------------------
    # 2. CONVICTION — must exceed equity threshold × multiplier
    # ------------------------------------------------------------------
    if abs(conviction) < conviction_threshold_used:
        return _reject("CONVICTION_TOO_LOW")

    # ------------------------------------------------------------------
    # 3. HORIZON — thesis must be long enough to justify options
    # ------------------------------------------------------------------
    if horizon_days < config.option_min_expiry_days:
        return _reject("HORIZON_TOO_SHORT")

    # ------------------------------------------------------------------
    # 4. CATALYST — a non-empty catalyst tag must be present
    # ------------------------------------------------------------------
    if not catalyst_tag:
        return _reject("NO_CATALYST")

    # ------------------------------------------------------------------
    # 5. IV CHECK — prefer IVR; cold-start falls back to realized vol proxy
    # ------------------------------------------------------------------
    ivr: float | None = None
    rvp: float | None = None
    try:
        ivr = iv_history.iv_rank(conn, underlying)
    except Exception:  # noqa: BLE001
        log.warning("gate: iv_rank() failed for %s; treating as cold-start", underlying)

    try:
        rvp = iv_history.realized_vol_proxy(conn, underlying)
    except Exception:  # noqa: BLE001
        log.warning("gate: realized_vol_proxy() failed for %s", underlying)

    if ivr is not None:
        # We have real IV-rank history — apply hard gate
        if ivr > config.option_ivr_max:
            return _reject("IV_RANK_TOO_HIGH", ivr=ivr, rvp=rvp)
    else:
        # Cold-start: IVR not available; use realized vol proxy as a soft
        # sanity bound.  We do NOT hard-block when proxy is also None — we
        # let the thesis through and record None so shadow data accumulates.
        # When proxy IS available and exceeds option_ivr_max (treating
        # realized-vol-% as an analogous "too expensive" signal), we reject.
        if rvp is not None and rvp > config.option_ivr_max:
            log.info(
                "gate: IVR history absent for %s; realized_vol_proxy=%.3f > ivr_max=%.2f → IV_RANK_TOO_HIGH",
                underlying,
                rvp,
                config.option_ivr_max,
            )
            return _reject("IV_RANK_TOO_HIGH", ivr=None, rvp=rvp)
        # No IVR and no decisive proxy — pass through with both recorded as-is
        log.info(
            "gate: cold-start IV check for %s (ivr=None, realized_vol_proxy=%s); passing through",
            underlying,
            rvp,
        )

    # ------------------------------------------------------------------
    # PASS — all criteria met
    # ------------------------------------------------------------------
    side = OptionSide.CALL if conviction > 0 else OptionSide.PUT

    return OptionGateDecision(
        express=True,
        reason="OK",
        side=side,
        target_delta_low=config.option_target_delta_low,
        target_delta_high=config.option_target_delta_high,
        min_expiry_days=config.option_min_expiry_days,
        catalyst_tag=catalyst_tag,
        conviction=conviction,
        conviction_threshold_used=conviction_threshold_used,
        horizon_days=horizon_days,
        ivr_estimate=ivr,
        realized_vol_proxy=rvp,
    )
