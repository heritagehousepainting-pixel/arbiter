"""Decision engine — Lane 12a.

Entry point: ``decide()``.

Flow per ticker/bucket:
1. Check gate → skip if disallowed
2. Map conviction to BUY / SELL / flat
3. Compute notional size via sizing.py
4. Skip if size == 0
5. Compute exits via exits.py
6. Build PaperOrder with ULID order_id and dedup_hash
"""
from __future__ import annotations

import dataclasses
from collections.abc import Callable
from datetime import date, datetime

from arbiter.contract.seams import FusionOutput, PaperOrder, TradingDecision
from arbiter.execution.idempotency import dedup_hash as _dedup_hash
from arbiter.policy.exits import compute_exits
from arbiter.policy.sizing import compute_size
from arbiter.types import HorizonBucket, OrderSide

try:
    from ulid import ULID  # type: ignore[import]

    def _new_ulid() -> str:
        return str(ULID())

except ImportError:
    import uuid

    def _new_ulid() -> str:  # type: ignore[misc]
        return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Conviction threshold to take a position
# ---------------------------------------------------------------------------

#: Minimum |conviction| to emit an order (avoid noise near zero).
_MIN_CONVICTION = 0.05


def _conviction_to_side(conviction: float) -> OrderSide | None:
    """Map a signed conviction value to BUY, SELL, or None (flat).

    Returns None when |conviction| < _MIN_CONVICTION.
    """
    if conviction >= _MIN_CONVICTION:
        return OrderSide.BUY
    if conviction <= -_MIN_CONVICTION:
        return OrderSide.SELL
    return None


def _build_advisor_signature(bucket_outputs: dict[HorizonBucket, FusionOutput], ticker: str) -> str:
    """Build a deterministic advisor signature from contributing advisor IDs."""
    # Collect all advisor IDs that contributed across all buckets for this ticker.
    ids: list[str] = []
    for fusion in bucket_outputs.values():
        ids.extend(sorted(fusion.advisor_contributions.keys()))
    return ",".join(sorted(set(ids))) or "no-advisors"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def decide(
    ticker: str,
    bucket_outputs: dict[HorizonBucket, FusionOutput],
    account: object,
    *,
    gate: Callable[[object, int], TradingDecision],
    adv_provider: Callable[[str, datetime], float | None],
    clock: object,
    config: object,
    portfolio_equity: float,
    live_advisor_count: int = 2,
    current_sector_exposure: float = 0.0,
    current_gross_exposure: float = 0.0,
    current_open_positions: int = 0,
    current_name_exposure: float = 0.0,
    entry_price: float = 100.0,
    trace: Callable[[str, dict], None] | None = None,
) -> list[PaperOrder]:
    """Produce PaperOrders for one ticker across all active horizon buckets.

    Parameters
    ----------
    ticker:
        Ticker symbol to decide on.
    bucket_outputs:
        Fusion results keyed by HorizonBucket.  Only buckets present in this
        dict are considered.
    account:
        Account object passed to the gate (opaque to policy).
    gate:
        Callable(account, live_advisor_count) → TradingDecision.
        This is the safety gate from Lane 4.
    adv_provider:
        Callable(ticker, as_of) → 20d ADV in USD, or None if unavailable.
        From Lane 3 PITGateway.
    clock:
        Clock object with a ``now() -> datetime`` method.
    config:
        Loaded Config with sizing caps.
    portfolio_equity:
        Current portfolio equity (USD).
    live_advisor_count:
        Number of live advisors active this cycle (passed to gate).
    current_sector_exposure:
        Already-committed sector notional (USD).
    current_gross_exposure:
        Already-committed gross notional (USD).
    current_open_positions:
        Number of currently open positions.
    current_name_exposure:
        Already-committed notional to THIS ticker (USD); nonzero only for an
        add-on to a held name (Tier-2 #5).  Sizing caps the add-on at the
        per-name headroom.
    entry_price:
        Reference price for stop-loss computation.  Callers should pass
        the PIT open price from Lane 3 (not market data directly).

    Returns
    -------
    list[PaperOrder]
        One PaperOrder per bucket that clears all filters.  Empty list when
        gate halts or no conviction survives sizing.
    """
    orders: list[PaperOrder] = []
    as_of: datetime = clock.now()
    entry_date: date = as_of.date()

    def _t(reason: str, **extra: object) -> None:
        # Trace is diagnostics-only: a broken callback must never abort decide.
        if trace is None:
            return
        try:
            trace("decide", {"reason": reason, "ticker": ticker, **extra})
        except Exception:  # noqa: BLE001, S110
            pass

    # Check gate once per ticker call (same account state for all buckets)
    gate_decision: TradingDecision = gate(account, live_advisor_count)
    if not gate_decision.allowed:
        _t("gate_blocked")
        return []

    advisor_signature = _build_advisor_signature(bucket_outputs, ticker)

    for bucket, fusion in bucket_outputs.items():
        side = _conviction_to_side(fusion.conviction)
        if side is None:
            _t(
                "flat_conviction",
                bucket=bucket.value,
                conviction=fusion.conviction,
            )
            continue  # flat — no position

        size = compute_size(
            fusion=fusion,
            portfolio_equity=portfolio_equity,
            config=config,
            gate_decision=gate_decision,
            adv_provider=adv_provider,
            ticker=ticker,
            as_of=as_of,
            current_sector_exposure=current_sector_exposure,
            current_gross_exposure=current_gross_exposure,
            current_open_positions=current_open_positions,
            current_name_exposure=current_name_exposure,
            trace=trace,
        )

        if size <= 0.0:
            _t("size_zero", bucket=bucket.value)
            continue

        exits = compute_exits(
            bucket=bucket,
            side=side,
            entry_price=entry_price,
            entry_date=entry_date,
        )

        # Build the order first (with a placeholder hash), then compute the
        # dedup_hash from the SINGLE SOURCE in idempotency.py so decision and
        # submit can never drift (D1 P2).  ``_dedup_hash`` reads only the
        # ticker/side/horizon/entry_date/advisor_signature fields.
        order = PaperOrder(
            order_id=_new_ulid(),
            dedup_hash="",
            ticker=ticker,
            side=side,
            qty=size,
            horizon_bucket=bucket,
            entry_date=entry_date,
            advisor_signature=advisor_signature,
            exits=exits,
        )
        order = dataclasses.replace(order, dedup_hash=_dedup_hash(order))
        orders.append(order)

    return orders


def decide_all(
    bucket_outputs_by_ticker: dict[str, dict[HorizonBucket, FusionOutput]],
    account: object,
    *,
    gate: Callable[[object, int], TradingDecision],
    adv_provider: Callable[[str, datetime], float | None],
    clock: object,
    config: object,
    portfolio_equity: float,
    live_advisor_count: int = 2,
    current_sector_exposure_by_ticker: dict[str, float] | None = None,
    current_gross_exposure: float = 0.0,
    current_open_positions: int = 0,
    entry_price_by_ticker: dict[str, float] | None = None,
    sector_by_ticker: dict[str, str] | None = None,
) -> list[PaperOrder]:
    """Run ``decide()`` across all tickers.

    Convenience wrapper that accumulates orders from multiple tickers,
    updating gross exposure and sector exposure as orders are added.

    Parameters
    ----------
    bucket_outputs_by_ticker:
        {ticker: {bucket: FusionOutput}}.
    current_sector_exposure_by_ticker:
        {ticker: current sector exposure USD}.  Defaults to 0 per ticker.
    entry_price_by_ticker:
        {ticker: entry price USD}.  Defaults to 100.0 per ticker.
    sector_by_ticker:
        {ticker: sector_name}.  If None or ticker absent, defaults to
        ``"UNKNOWN"`` — a single sector so the 20% cap still binds across
        the batch as a conservative default.

    All other parameters are the same as ``decide()``.
    """
    all_orders: list[PaperOrder] = []
    running_gross = current_gross_exposure
    running_open = current_open_positions
    # Accumulate sector exposure within this batch keyed by sector name.
    running_sector: dict[str, float] = {}

    for ticker, bucket_outputs in bucket_outputs_by_ticker.items():
        # Sector: caller-supplied or conservative "UNKNOWN" default.
        sector = (sector_by_ticker or {}).get(ticker, "UNKNOWN")

        # Starting sector exposure = pre-existing + accumulated this batch.
        pre_existing_sector = (current_sector_exposure_by_ticker or {}).get(ticker, 0.0)
        current_sector = pre_existing_sector + running_sector.get(sector, 0.0)

        price = (entry_price_by_ticker or {}).get(ticker, 100.0)

        orders = decide(
            ticker=ticker,
            bucket_outputs=bucket_outputs,
            account=account,
            gate=gate,
            adv_provider=adv_provider,
            clock=clock,
            config=config,
            portfolio_equity=portfolio_equity,
            live_advisor_count=live_advisor_count,
            current_sector_exposure=current_sector,
            current_gross_exposure=running_gross,
            current_open_positions=running_open,
            entry_price=price,
        )

        all_orders.extend(orders)
        order_notional = sum(o.qty for o in orders)
        running_gross += order_notional
        running_open += len(orders)
        # Roll sector accumulator for this cycle.
        running_sector[sector] = running_sector.get(sector, 0.0) + order_notional

    return all_orders
