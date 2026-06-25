"""Options expression orchestrator — the ONLY entry point the engine calls.

Path: gate → contract selection → sizing → shadow log (→ paper order in P2).

Isolation guarantees
--------------------
- ``config.options_mode == "off"`` (default) → immediate ``return None`` (one
  attribute read; zero behavioural change to the equity path).
- **Shadow mode never folds delta into the live ``RiskBook``.** Folding would
  change the equity ``decide`` caps for later ideas in the SAME cycle — a
  behavioural change. Shadow only *records* the would-be ``delta_adjusted_
  notional`` in the shadow row. The real fold happens in P2 (paper mode) when a
  contract is actually bought.
- Any error is logged and swallowed — the equity path must never be disrupted
  by the options overlay.
"""
from __future__ import annotations

import datetime
import sqlite3
from typing import Optional

import structlog

from arbiter.config import Config
from arbiter.options.alpaca_options_client import AlpacaOptionsClient, OptionsBrokerError
from arbiter.options.contract_selector import select_contract
from arbiter.options.gate import options_expression_gate
from arbiter.options.positions import record_open_position
from arbiter.options.shadow_log import log_shadow_option
from arbiter.options.sizing import size_option
from arbiter.policy.book import RiskBook
from arbiter.policy.decision import _MIN_CONVICTION as _EQUITY_ENTRY_THRESHOLD

log = structlog.get_logger(__name__)


def _dominant_catalyst(fusion_output: object) -> Optional[str]:
    """Return the advisor_id contributing most to the conviction = the catalyst.

    Uses ``FusionOutput.advisor_contributions`` (advisor_id → signed float). The
    dominant smart-money source (``A1.activist``, ``A1.insider``, ``A1.fund``,
    ``A3.news`` …) IS the catalyst for the option play. Returns ``None`` when
    there are no contributions (→ the gate rejects on ``NO_CATALYST``).
    """
    contribs = getattr(fusion_output, "advisor_contributions", None) or {}
    if not contribs:
        return None
    top_id, top_val = max(contribs.items(), key=lambda kv: abs(kv[1]))
    return top_id if abs(top_val) > 0 else None


def _shadow_already_logged(conn: sqlite3.Connection, idea_id: str) -> bool:
    """True if this idea already has a shadow row (dedup across cycles)."""
    row = conn.execute(
        "SELECT 1 FROM option_shadow_log WHERE idea_id = ? LIMIT 1", (idea_id,)
    ).fetchone()
    return row is not None


def _position_open_for_idea(conn: sqlite3.Connection, idea_id: str) -> bool:
    """True if this idea already has an OPEN paper option position.

    Open = an ``option_positions`` row with no matching ``option_outcomes`` row
    (openness is derived from the absence of an outcome — fully insert-only).
    """
    row = conn.execute(
        "SELECT 1 FROM option_positions p "
        "LEFT JOIN option_outcomes o "
        "  ON o.idea_id = p.idea_id AND o.occ_symbol = p.occ_symbol "
        "WHERE p.idea_id = ? AND o.id IS NULL LIMIT 1",
        (idea_id,),
    ).fetchone()
    return row is not None


def express_option(
    conn: sqlite3.Connection,
    idea: object,
    fusion_output: object,
    *,
    config: Config,
    book_container: list[RiskBook],
    clock: object,
    portfolio_equity: float,
    open_options_premium: float,
    current_price_provider: object,
    client: Optional[AlpacaOptionsClient] = None,
) -> Optional[str]:
    """Orchestrate the options expression path for one idea.

    Returns the shadow-log row id (shadow mode) or the open-position id (paper
    mode) when a candidate option play is recorded/placed, else ``None`` (layer
    off, no live spot, gate reject, no liquid contract, can't afford the sleeve,
    already recorded, or any error).

    ``book_container`` is the engine's ``_book: list[RiskBook]`` accumulator.
    SHADOW mode never touches it (isolation). PAPER mode folds the option's
    delta-adjusted notional into ``book_container[0]`` after a successful
    placement, so later equity ideas this cycle see the real exposure.
    """
    # --- isolation: total no-op when off ---
    if config.options_mode == "off":
        return None

    try:
        underlying = idea.ticker  # type: ignore[attr-defined]
        conviction = float(fusion_output.conviction)  # type: ignore[attr-defined]
        horizon_days = int(idea.horizon_days)  # type: ignore[attr-defined]
        catalyst_tag = _dominant_catalyst(fusion_output)

        # Live underlying spot. None in sim/backtest/closed-market → layer inert
        # (correct: the options overlay is a live-only expression).
        underlying_price = current_price_provider.current_price(underlying)  # type: ignore[attr-defined]
        if underlying_price is None or underlying_price <= 0:
            return None

        if client is None:
            client = AlpacaOptionsClient(config)

        now = clock.now()  # type: ignore[attr-defined]
        as_of_iso = now.isoformat()

        gate_decision = options_expression_gate(
            conn,
            client,
            underlying=underlying,
            conviction=conviction,
            horizon_days=horizon_days,
            catalyst_tag=catalyst_tag,
            equity_entry_threshold=_EQUITY_ENTRY_THRESHOLD,
            underlying_price=underlying_price,
            config=config,
            as_of=as_of_iso,
        )
        if not gate_decision.express:
            log.debug(
                "options.express.gate_reject",
                ticker=underlying,
                reason=gate_decision.reason,
            )
            return None

        contract = select_contract(
            client,
            gate_decision,
            underlying=underlying,
            horizon_days=horizon_days,
            config=config,
            as_of=now.date(),
        )
        if contract is None:
            log.info("options.express.no_contract", ticker=underlying)
            return None

        order = size_option(
            contract,
            portfolio_equity=portfolio_equity,
            open_options_premium=open_options_premium,
            underlying_price=underlying_price,
            config=config,
        )
        if order is None:
            log.info("options.express.unaffordable", ticker=underlying)
            return None

        idea_id = idea.idea_id  # type: ignore[attr-defined]

        # --- SHADOW: record the would-be play; never touch the equity book. ---
        if config.options_mode == "shadow":
            if _shadow_already_logged(conn, idea_id):  # one shadow row per idea
                return None
            shadow_id = log_shadow_option(
                conn,
                idea_id=idea_id,
                gate_decision=gate_decision,
                contract=contract,
                order=order,
                as_of=as_of_iso,
                created_at=as_of_iso,
            )
            log.info(
                "options.express.shadow_logged",
                ticker=underlying,
                occ=contract.occ_symbol,
                contracts=order.contracts_qty,
                est_premium=order.est_premium,
                delta_notional=order.delta_adjusted_notional,
                catalyst=catalyst_tag,
            )
            return shadow_id

        # --- PAPER: place the (paper) order, fold delta, record the position. ---
        if config.options_mode == "paper":
            if _position_open_for_idea(conn, idea_id):  # one open position per idea
                return None
            try:
                resp = client.place(order)
            except OptionsBrokerError as exc:
                # Swallow — the equity path must never be disrupted, and a failed
                # placement must NOT consume sleeve/risk budget.
                log.error("options.express.place_failed", ticker=underlying, error=str(exc))
                return None

            # Fold delta into the live book ONLY after a confirmed placement.
            book_container[0] = book_container[0].add_option_delta(
                underlying, order.delta_adjusted_notional
            )
            thesis_horizon_date = now.date() + datetime.timedelta(days=horizon_days)
            pos_id = record_open_position(
                conn,
                idea_id=idea_id,
                shadow_id=None,
                contract=contract,
                order=order,
                broker_order_id=str(resp.get("id") or ""),
                underlying_open_price=underlying_price,
                thesis_horizon_date=thesis_horizon_date,
                original_conviction=conviction,
                open_ts=as_of_iso,
                created_at=as_of_iso,
            )
            log.info(
                "options.express.paper_opened",
                ticker=underlying,
                occ=contract.occ_symbol,
                contracts=order.contracts_qty,
                broker_order_id=resp.get("id"),
                delta_notional=order.delta_adjusted_notional,
            )
            return pos_id

        return None

    except Exception as exc:  # never disrupt the equity path
        log.warning(
            "options.express.failed",
            ticker=getattr(idea, "ticker", None),
            error=str(exc),
        )
        return None
