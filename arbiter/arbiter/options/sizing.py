"""Option order sizing — options expression layer, P1.

Sizing constraints
------------------
1. **Options sleeve cap**: aggregate premium at risk across all open option
   positions must not exceed ``portfolio_equity × config.options_sleeve_pct``
   (default 35%).  This is a *premium* cap; not a delta-exposure cap (that is
   handled separately by the ``RiskBook`` delta fold).

2. **Delta-adjusted notional** is computed here and returned in ``OptionOrder``
   so that the caller can immediately fold it into ``RiskBook.add_option_delta()``.
   Formula: ``|delta| × 100 × underlying_price × contracts_qty``

3. **Minimum 1 contract**: if the budget doesn't support even 1 contract
   (``remaining_sleeve < mid_price × 100``), return None — the caller skips
   submission and logs the rejection.

4. **Integer contracts only**: ``contracts_qty = floor(remaining_sleeve /
   (mid_price × 100))``, clamped to ≥ 1 on the lower end.

The sleeve budget remaining is passed in by the caller (``express.py``), which
tracks the aggregate premium of all currently-open option positions against the
current portfolio equity.
"""
from __future__ import annotations

import math
from typing import Optional

from arbiter.config import Config
from arbiter.options.types import OptionContract, OptionOrder


def size_option(
    contract: OptionContract,
    *,
    portfolio_equity: float,
    open_options_premium: float,
    underlying_price: float,
    config: Config,
) -> Optional[OptionOrder]:
    """Size an option order within the sleeve budget.

    Parameters
    ----------
    contract : OptionContract
        The selected contract (output of ``select_contract()``).
    portfolio_equity : float
        Current total portfolio equity (USD).  Used to compute the sleeve
        ceiling: ``portfolio_equity × config.options_sleeve_pct``.
    open_options_premium : float
        Aggregate premium already at risk in open option positions (USD).
        The available budget is
        ``(portfolio_equity × options_sleeve_pct) - open_options_premium``.
    underlying_price : float
        Current price of the underlying equity (USD).  Used to compute
        delta-adjusted notional.
    config : Config
        Frozen arbiter config.  Reads ``options_sleeve_pct`` and uses
        ``contract.delta`` for the notional calculation.

    Returns
    -------
    OptionOrder | None
        A fully-sized ``OptionOrder`` ready for submission or shadow logging,
        or None when the remaining sleeve budget is too small for even 1
        contract, or when the contract has no valid mid price.
    """
    mid = contract.mid_price
    # Guard: no valid bid/ask — cannot size.
    if mid is None or mid <= 0.0:
        return None

    # Guard: delta required for notional calculation.
    if contract.delta is None:
        return None

    # 1. Per-contract premium cost (one contract = 100 share equivalents).
    cost_per_contract: float = mid * 100.0

    # 2. Remaining sleeve budget.
    sleeve_ceiling: float = config.options_sleeve_pct * portfolio_equity
    remaining: float = sleeve_ceiling - open_options_premium

    # 3. How many whole contracts fit in the remaining budget?
    contracts_qty: int = math.floor(remaining / cost_per_contract)

    # 4. If we can't afford even one contract, reject.
    if contracts_qty < 1:
        return None

    # 5. Estimated total premium outlay.
    est_premium: float = contracts_qty * cost_per_contract

    # 6. Delta-adjusted notional: |delta| × 100 × underlying_price × contracts_qty
    delta_adjusted_notional: float = (
        abs(contract.delta) * 100.0 * underlying_price * contracts_qty
    )

    return OptionOrder(
        contract=contract,
        contracts_qty=contracts_qty,
        est_premium=est_premium,
        delta_adjusted_notional=delta_adjusted_notional,
        side=contract.side,
    )
