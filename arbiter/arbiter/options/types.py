"""Frozen dataclasses and enums for the options expression layer.

ALL types here are pure data containers — no logic, no I/O.  Every field
has a docstring comment explaining its contract so the parallel implementation
wave can fill stubs without re-reading the plan.

Design constraints
------------------
- ``OptionContract`` mirrors the Alpaca ``indicative`` feed response shape;
  field names are chosen to survive a direct ``**snapshot_dict`` unpack.
- ``OptionGateDecision`` carries enough context for the shadow log: if the
  gate fires the full call-chain can be reconstructed from this object.
- ``OptionShadowRow`` / ``OptionOutcomeRow`` are the DB-row shapes; they are
  the source-of-truth mapping between Python objects and SQL columns —
  migrations are authored to match these field lists exactly.
"""
from __future__ import annotations

import datetime
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class OptionSide(str, Enum):
    """Whether the option is a call or a put.

    Maps directly to Alpaca's ``type`` field: ``"call"`` / ``"put"``.
    """
    CALL = "call"
    PUT = "put"


# ---------------------------------------------------------------------------
# Market-data types (sourced from Alpaca ``indicative`` snapshot)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionContract:
    """A single options contract as returned by the Alpaca snapshot feed.

    All greeks and market-data fields may be ``None`` when Alpaca returns
    null (e.g. very deep ITM/OTM contracts or < 1 DTE) — callers must
    guard.  The ``select_contract`` function filters these out before
    returning a candidate.

    Fields
    ------
    occ_symbol : str
        Full OCC option symbol, e.g. ``"AAPL240119C00150000"``.
        This is the identifier used for all Alpaca order submissions.
    underlying : str
        The underlying equity ticker, e.g. ``"AAPL"``.
    side : OptionSide
        CALL or PUT.
    strike : float
        Strike price in USD.
    expiry : datetime.date
        Expiration date (date only, no time component).
    delta : float | None
        Option delta from the Alpaca greeks snapshot; None when unavailable.
        The contract selector requires delta ∈ [0.70, 0.80] for calls
        (or [-0.80, -0.70] for puts — Alpaca signs put delta negative).
    iv : float | None
        Implied volatility (annualised, as a decimal, e.g. 0.38 = 38%).
        None when unavailable.
    bid : float | None
        Best bid price in USD.  None when market is closed / unavailable.
    ask : float | None
        Best ask price in USD.  None when market is closed / unavailable.
    open_interest : int | None
        Open interest in contracts.  Liquidity gate requires ≥ 100.
    volume : int | None
        Daily volume in contracts.  Liquidity gate requires ≥ 10.
    """
    occ_symbol: str
    underlying: str
    side: OptionSide
    strike: float
    expiry: datetime.date
    delta: Optional[float]
    iv: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    open_interest: Optional[int]
    volume: Optional[int]

    @property
    def mid_price(self) -> Optional[float]:
        """Mid-point of the bid/ask spread, or None when either is missing."""
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> Optional[float]:
        """Bid/ask spread as a fraction of the mid price.

        Used as a secondary liquidity indicator; not yet wired to a gate
        threshold (logged in shadow for later calibration).
        """
        mid = self.mid_price
        if mid is None or mid == 0.0:
            return None
        return (self.ask - self.bid) / mid  # type: ignore[operator]

    def dte(self, as_of: datetime.date) -> int:
        """Calendar days to expiry from an EXPLICIT reference date.

        Takes ``as_of`` rather than reading ``date.today()`` — the system never
        reads wall-clock time outside the injected clock (no-lookahead rule).
        """
        return (self.expiry - as_of).days


# ---------------------------------------------------------------------------
# Gate decision
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionGateDecision:
    """Result of ``options_expression_gate()``.

    Carries both the binary express/reject decision AND enough diagnostic
    context to write a rich shadow-log row.  When ``express=False``, the
    ``reason`` field explains which gate criterion failed (useful for
    calibrating threshold tuning from shadow logs).

    Fields
    ------
    express : bool
        True → the thesis qualifies for options expression; False → blocked.
    reason : str
        Human-readable gate outcome, e.g. ``"OK"`` or ``"IV_RANK_TOO_HIGH"``.
        Parallel wave: use a fixed vocabulary of short codes so they can be
        queried via SQL (``GROUP BY reason``).
    side : OptionSide | None
        CALL for bullish, PUT for bearish.  None when express=False.
    target_delta_low : float
        Lower bound of acceptable delta range (default 0.70 from config).
    target_delta_high : float
        Upper bound of acceptable delta range (default 0.80 from config).
    min_expiry_days : int
        Minimum DTE required for contract selection (config: option_min_expiry_days).
    catalyst_tag : str | None
        The catalyst that qualified the thesis (e.g. ``"13D"``, ``"form4_cluster"``).
        None when the gate rejected on missing catalyst.
    conviction : float
        The conviction score that was evaluated (from fusion_output).
    conviction_threshold_used : float
        The threshold applied: equity_threshold × option_conviction_mult.
    horizon_days : float
        The thesis horizon in days (from the idea).
    ivr_estimate : float | None
        The IV-rank / proxy used (None when the gate rejected before reaching
        the IV check, or when no estimate was available).
    realized_vol_proxy : float | None
        Realized volatility estimate used as IV-rank proxy in P1 cold-start.
    """
    express: bool
    reason: str
    side: Optional[OptionSide]
    target_delta_low: float
    target_delta_high: float
    min_expiry_days: int
    catalyst_tag: Optional[str]
    conviction: float
    conviction_threshold_used: float
    horizon_days: float
    ivr_estimate: Optional[float]
    realized_vol_proxy: Optional[float]


# ---------------------------------------------------------------------------
# Option order (P2 paper execution)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OptionOrder:
    """A fully-sized option order ready for submission to Alpaca.

    This is the output of ``sizing.size_option()`` and the input to
    ``AlpacaOptionsClient.place()``.

    Fields
    ------
    contract : OptionContract
        The selected contract (output of ``select_contract()``).
    contracts_qty : int
        Number of option contracts to buy (each = 100 share equivalents).
        Always ≥ 1; sizing rejects if budget doesn't cover 1 contract.
    est_premium : float
        Estimated total premium outlay = ``contracts_qty × mid_price × 100``.
        Used for sleeve-budget accounting.
    delta_adjusted_notional : float
        ``|delta| × 100 × underlying_price × contracts_qty`` in USD.
        This is the value folded into ``RiskBook.add_option_delta()`` so that
        gross/sector caps remain binding even when premium is small.
    side : OptionSide
        Redundant with ``contract.side`` but surfaced here for fast access
        without traversing the nested object.
    """
    contract: OptionContract
    contracts_qty: int
    est_premium: float
    delta_adjusted_notional: float
    side: OptionSide


# ---------------------------------------------------------------------------
# Shadow log row (DB persistence shape for P1)
# ---------------------------------------------------------------------------

@dataclass
class OptionShadowRow:
    """Mutable builder for an ``option_shadow_log`` DB row.

    Use this as a plain dict-like structure, then call ``to_dict()`` before
    passing to ``insert_row()``.  Mutable (not frozen) so callers can build
    incrementally.

    Column mapping
    --------------
    All field names match SQL column names in ``029_options_shadow.sql``.

    Fields
    ------
    id : str
        ULID primary key.  Generated by ``log_shadow_option()`` via
        ``generate_ulid()``.
    idea_id : str
        FK → ``ideas.idea_id``.
    underlying : str
        Ticker of the underlying equity.
    as_of : str
        Tz-aware UTC ISO timestamp when the shadow decision was made.
    gate_express : int
        1 if the gate fired; 0 if it rejected.  Stored as INTEGER for easy
        SQL aggregation.
    gate_reason : str
        Short reason code from OptionGateDecision.reason.
    side : str | None
        ``"call"`` or ``"put"``; None when gate_express=0.
    occ_symbol : str | None
        Selected contract OCC symbol; None when gate_express=0 or no
        contract found.
    strike : float | None
        Strike price of the selected contract.
    expiry : str | None
        ISO date string of the selected contract's expiry.
    delta : float | None
        Delta of the selected contract at snapshot time.
    iv : float | None
        Implied volatility of the selected contract at snapshot time.
    bid : float | None
        Bid at snapshot time.
    ask : float | None
        Ask at snapshot time.
    open_interest : int | None
        Open interest of the selected contract.
    volume : int | None
        Volume of the selected contract.
    est_premium : float | None
        Estimated premium outlay (gate+sizing fired).
    delta_adjusted_notional : float | None
        Delta-adjusted notional (gate+sizing fired).
    contracts_qty : int | None
        Number of contracts sized (gate+sizing fired).
    conviction : float
        Conviction score evaluated by the gate.
    horizon_days : float
        Thesis horizon in days.
    catalyst_tag : str | None
        Catalyst tag from the gate decision.
    ivr_estimate : float | None
        IV rank / proxy from the gate.
    realized_vol_proxy : float | None
        Realized vol proxy from the gate.
    created_at : str
        Tz-aware UTC ISO insertion timestamp.
    """
    id: str
    idea_id: str
    underlying: str
    as_of: str
    gate_express: int
    gate_reason: str
    side: Optional[str]
    occ_symbol: Optional[str]
    strike: Optional[float]
    expiry: Optional[str]
    delta: Optional[float]
    iv: Optional[float]
    bid: Optional[float]
    ask: Optional[float]
    open_interest: Optional[int]
    volume: Optional[int]
    est_premium: Optional[float]
    delta_adjusted_notional: Optional[float]
    contracts_qty: Optional[int]
    conviction: float
    horizon_days: float
    catalyst_tag: Optional[str]
    ivr_estimate: Optional[float]
    realized_vol_proxy: Optional[float]
    created_at: str

    def to_dict(self) -> dict:
        """Return a plain dict suitable for ``insert_row()``."""
        return {
            "id": self.id,
            "idea_id": self.idea_id,
            "underlying": self.underlying,
            "as_of": self.as_of,
            "gate_express": self.gate_express,
            "gate_reason": self.gate_reason,
            "side": self.side,
            "occ_symbol": self.occ_symbol,
            "strike": self.strike,
            "expiry": self.expiry,
            "delta": self.delta,
            "iv": self.iv,
            "bid": self.bid,
            "ask": self.ask,
            "open_interest": self.open_interest,
            "volume": self.volume,
            "est_premium": self.est_premium,
            "delta_adjusted_notional": self.delta_adjusted_notional,
            "contracts_qty": self.contracts_qty,
            "conviction": self.conviction,
            "horizon_days": self.horizon_days,
            "catalyst_tag": self.catalyst_tag,
            "ivr_estimate": self.ivr_estimate,
            "realized_vol_proxy": self.realized_vol_proxy,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# IV history row (DB persistence shape for daily ATM-IV snapshots)
# ---------------------------------------------------------------------------

@dataclass
class IVHistoryRow:
    """Mutable builder for an ``option_iv_history`` DB row.

    Written daily (once per underlying per cycle) so that IV-rank is
    computable by P2 from locally-accumulated data.

    Fields
    ------
    id : str
        ULID primary key.
    underlying : str
        Ticker of the underlying.
    as_of : str
        Tz-aware UTC ISO timestamp of the snapshot.
    atm_iv : float
        ATM implied volatility at snapshot time (the IV of the nearest-ATM
        contract with ≥ 60 DTE, as selected by the snapshot logic in
        ``iv_history.record_iv_snapshot()``).
    occ_symbol : str
        The OCC symbol of the contract used as the ATM proxy.
    created_at : str
        Tz-aware UTC ISO insertion timestamp.
    """
    id: str
    underlying: str
    as_of: str
    atm_iv: float
    occ_symbol: str
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "underlying": self.underlying,
            "as_of": self.as_of,
            "atm_iv": self.atm_iv,
            "occ_symbol": self.occ_symbol,
            "created_at": self.created_at,
        }


# ---------------------------------------------------------------------------
# Option outcome row (ISOLATED from equity outcomes — P2)
# ---------------------------------------------------------------------------

@dataclass
class OptionOutcomeRow:
    """Mutable builder for an ``option_outcomes`` DB row.

    ISOLATION CONTRACT: rows in this table NEVER reach ``run_outcome_sweep()``
    or any equity trust/learning path.  ``option_pl_pct`` is display-only;
    ``underlying_alpha_bps`` is the only field that may later be cross-referenced
    with the equity trust ledger (for direction-only validation, not P&L).

    Fields
    ------
    id : str
        ULID primary key.
    shadow_id : str | None
        FK → ``option_shadow_log.id`` (the shadow row this outcome resolves;
        None for P2 live orders that were never shadowed).
    idea_id : str
        FK → ``ideas.idea_id``.
    underlying : str
        Ticker of the underlying equity.
    occ_symbol : str
        OCC symbol of the contract.
    side : str
        ``"call"`` or ``"put"``.
    open_ts : str
        Tz-aware UTC ISO timestamp when the position was opened.
    close_ts : str
        Tz-aware UTC ISO timestamp when the position was closed.
    close_reason : str
        Short code: ``"premium_stop"`` | ``"horizon_expiry"`` |
        ``"conviction_reversal"`` | ``"expiry_approach"`` | ``"manual"``.
    entry_premium : float
        Total premium paid to open (USD, negative cash flow).
    exit_premium : float
        Total premium received on close (USD, positive cash flow).
    option_pl_pct : float
        Option P&L as a fraction of entry premium:
        ``(exit_premium - entry_premium) / entry_premium``.
        Display-only; does NOT feed advisor trust scores.
    underlying_alpha_bps : float
        Underlying equity move from open to close (basis points).
        ``(underlying_close_price / underlying_open_price - 1) * 10_000``.
        This is the direction-validation bridge to the equity trust ledger.
    delta_at_open : float | None
        Contract delta at position open.
    iv_at_open : float | None
        Implied volatility at position open.
    iv_at_close : float | None
        Implied volatility at position close.
    contracts_qty : int
        Number of contracts held.
    created_at : str
        Tz-aware UTC ISO insertion timestamp.
    """
    id: str
    shadow_id: Optional[str]
    idea_id: str
    underlying: str
    occ_symbol: str
    side: str
    open_ts: str
    close_ts: str
    close_reason: str
    entry_premium: float
    exit_premium: float
    option_pl_pct: float
    underlying_alpha_bps: float
    delta_at_open: Optional[float]
    iv_at_open: Optional[float]
    iv_at_close: Optional[float]
    contracts_qty: int
    created_at: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "shadow_id": self.shadow_id,
            "idea_id": self.idea_id,
            "underlying": self.underlying,
            "occ_symbol": self.occ_symbol,
            "side": self.side,
            "open_ts": self.open_ts,
            "close_ts": self.close_ts,
            "close_reason": self.close_reason,
            "entry_premium": self.entry_premium,
            "exit_premium": self.exit_premium,
            "option_pl_pct": self.option_pl_pct,
            "underlying_alpha_bps": self.underlying_alpha_bps,
            "delta_at_open": self.delta_at_open,
            "iv_at_open": self.iv_at_open,
            "iv_at_close": self.iv_at_close,
            "contracts_qty": self.contracts_qty,
            "created_at": self.created_at,
        }
