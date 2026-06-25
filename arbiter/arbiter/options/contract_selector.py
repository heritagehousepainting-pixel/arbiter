"""Contract selector for the options expression layer.

Selection policy (locked by plan 2026-06-25)
--------------------------------------------
- **Target delta: 0.70 – 0.80 (deep ITM).**  Selected for equity-like delta
  (high directional efficiency), minimal theta decay, and best IV-crush
  resistance for slow disclosure theses.  Tunable via config.
- **Expiry window:**
      min_expiry = as_of + max(option_min_expiry_days, horizon_days + option_horizon_buffer_days)
      max_expiry = as_of + horizon_days + option_max_expiry_buffer_days
  The option must NOT expire during the expected holding period.
- **Side:** CALL for bullish, PUT for bearish (from the gate decision).
- **Liquidity post-filter:** open_interest >= option_min_open_interest,
  volume >= option_min_volume; contracts below these thresholds are excluded.

Selection algorithm
-------------------
1. Guard: if gate_decision.express is False, return None immediately.
2. Compute expiry window as above.
3. Fetch chain via AlpacaOptionsClient.fetch_chain().
4. Filter: delta not None; open_interest and volume meet floors;
   |delta| within [target_delta_low, target_delta_high].
5. Sort by |abs(delta) - target_delta_mid| ascending (closest to midpoint ~0.75);
   tie-break by tightest spread_pct.
6. Return contracts[0] or None.
"""
from __future__ import annotations

import datetime
import logging
from typing import Optional

from arbiter.config import Config
from arbiter.options.alpaca_options_client import AlpacaOptionsClient
from arbiter.options.types import OptionContract, OptionGateDecision

log = logging.getLogger(__name__)


def select_contract(
    client: AlpacaOptionsClient,
    gate_decision: OptionGateDecision,
    *,
    underlying: str,
    horizon_days: float,
    config: Config,
    as_of: datetime.date,
) -> Optional[OptionContract]:
    """Select the best-matching options contract for a qualifying thesis.

    Only called when ``gate_decision.express is True``.  Returns None when
    the chain is empty or no contract survives the delta / liquidity filters
    (the caller should log a ``NO_CONTRACT_FOUND`` outcome in the shadow log).

    Parameters
    ----------
    client : AlpacaOptionsClient
        Initialised options data client.
    gate_decision : OptionGateDecision
        The gate decision (must have ``express=True``); used for ``side``,
        ``target_delta_low``, ``target_delta_high``, and ``min_expiry_days``.
    underlying : str
        Equity ticker.
    horizon_days : float
        Thesis horizon in calendar days (from the ``Idea``).
    config : Config
        Frozen arbiter config.
    as_of : datetime.date
        Reference date for expiry calculation (injected by caller).

    Returns
    -------
    OptionContract | None
        The selected contract, or None if no qualifying contract found.
    """
    # Guard — should not be called when gate did not pass, but be defensive
    if not gate_decision.express:
        return None

    # ------------------------------------------------------------------
    # Expiry window computation
    # ------------------------------------------------------------------
    # min_expiry: must be at least option_min_expiry_days out AND at least
    # horizon_days + buffer beyond as_of (so the option lives past the thesis)
    min_dte = max(
        config.option_min_expiry_days,
        int(horizon_days) + config.option_horizon_buffer_days,
    )
    min_expiry = as_of + datetime.timedelta(days=min_dte)
    max_expiry = as_of + datetime.timedelta(
        days=int(horizon_days) + config.option_max_expiry_buffer_days
    )

    # Guard: max_expiry must be >= min_expiry (degenerate config check)
    if max_expiry < min_expiry:
        log.warning(
            "select_contract: max_expiry %s < min_expiry %s for %s (horizon=%.0f days); returning None",
            max_expiry,
            min_expiry,
            underlying,
            horizon_days,
        )
        return None

    # ------------------------------------------------------------------
    # Fetch chain
    # ------------------------------------------------------------------
    try:
        chain = client.fetch_chain(
            underlying,
            min_expiry=min_expiry,
            max_expiry=max_expiry,
            side=gate_decision.side,
            # A 5-month window on a liquid underlying spans hundreds of strikes
            # (NVDA ~400 in-window). The default limit (100) truncates the chain
            # and can cut off the deep-ITM 0.70-0.80 delta band entirely → a
            # silent no_contract. Request enough to cover large chains.
            limit=1500,
        )
    except Exception:  # noqa: BLE001
        log.warning("select_contract: fetch_chain() failed for %s", underlying, exc_info=True)
        return None

    if not chain:
        log.info("select_contract: empty chain for %s (%s)", underlying, gate_decision.side)
        return None

    # ------------------------------------------------------------------
    # Filter candidates
    # ------------------------------------------------------------------
    delta_low = gate_decision.target_delta_low    # e.g. 0.70
    delta_high = gate_decision.target_delta_high  # e.g. 0.80
    min_oi = config.option_min_open_interest      # 100
    min_vol = config.option_min_volume            # 10

    candidates: list[OptionContract] = []
    for contract in chain:
        # Must have delta, open_interest, and bid/ask for a usable contract.
        if contract.delta is None:
            continue
        if contract.open_interest is None or contract.open_interest < min_oi:
            continue
        # Volume is the PRIMARY-but-unreliable signal: Alpaca's contracts
        # endpoint frequently returns volume=None (and long-dated LEAPS trade
        # thinly day-to-day), so open_interest is the binding liquidity check.
        # Reject on volume ONLY when it is present and below the floor — a
        # missing volume must NOT veto an otherwise deep-OI contract (this made
        # the whole layer inert in live shadow testing).
        if contract.volume is not None and contract.volume < min_vol:
            continue
        if contract.bid is None or contract.ask is None:
            continue

        # For both calls and puts use absolute delta for band comparison
        abs_delta = abs(contract.delta)
        if not (delta_low <= abs_delta <= delta_high):
            continue

        candidates.append(contract)

    if not candidates:
        log.info(
            "select_contract: no contracts survived filters for %s "
            "(chain_size=%d, delta_band=[%.2f, %.2f], min_oi=%d, min_vol=%d)",
            underlying,
            len(chain),
            delta_low,
            delta_high,
            min_oi,
            min_vol,
        )
        return None

    # ------------------------------------------------------------------
    # Rank: closest |delta| to midpoint, tie-break by tightest spread_pct
    # ------------------------------------------------------------------
    target_delta_mid = (delta_low + delta_high) / 2.0  # default 0.75

    def _sort_key(c: OptionContract) -> tuple[float, float]:
        delta_distance = abs(abs(c.delta) - target_delta_mid)  # type: ignore[arg-type]
        spread = c.spread_pct if c.spread_pct is not None else float("inf")
        return (delta_distance, spread)

    candidates.sort(key=_sort_key)
    best = candidates[0]

    log.info(
        "select_contract: selected %s (delta=%.3f, oi=%d, vol=%d, expiry=%s)",
        best.occ_symbol,
        best.delta,  # type: ignore[arg-type]
        best.open_interest,  # type: ignore[arg-type]
        best.volume,  # type: ignore[arg-type]
        best.expiry,
    )
    return best
