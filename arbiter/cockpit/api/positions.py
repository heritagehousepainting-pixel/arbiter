"""Live open-positions + portfolio stats (read-only, from Alpaca).

Reuses the arbiter ``AlpacaAdapter`` HTTP plumbing to fetch the RAW
``/v2/positions`` payload (which carries current_price / unrealized_pl /
unrealized_plpc / cost_basis that ``get_positions`` drops) and ``/v2/account``.
Strictly read-only; degrades gracefully when Alpaca is unreachable.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .contract import OpenPosition, Portfolio, PositionsResponse
from .db import DEFAULT_DB_PATH

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # repo root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_positions() -> PositionsResponse:
    """Return live open positions + aggregate portfolio stats."""
    try:
        import sys  # noqa: PLC0415
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.engine import build_executor  # noqa: PLC0415

        ex = build_executor(load_config())
        # Raw positions payload (full fields) via the adapter's HTTP plumbing.
        raw = ex.http_get(f"{ex._base()}/v2/positions", ex._headers())  # type: ignore[attr-defined]
        acct = ex.get_account()
    except Exception:
        return PositionsResponse(as_of=_now(), alpaca_ok=False)

    positions: list[OpenPosition] = []
    n_long = n_short = 0
    gross = net = total_cost = total_upl = 0.0

    for p in raw or []:
        ticker = p.get("symbol", "")
        if not ticker:
            continue
        qty_signed = _f(p.get("qty"), 0.0) or 0.0
        side = "short" if qty_signed < 0 else "long"
        avg_entry = _f(p.get("avg_entry_price"), 0.0) or 0.0
        current = _f(p.get("current_price"))
        mv = _f(p.get("market_value"))
        cost_basis = _f(p.get("cost_basis"))
        if cost_basis is None:
            cost_basis = abs(qty_signed) * avg_entry
        upl = _f(p.get("unrealized_pl"))
        upl_pct = _f(p.get("unrealized_plpc"))  # Alpaca returns a fraction

        positions.append(OpenPosition(
            ticker=ticker, side=side, qty=abs(qty_signed), avg_entry=avg_entry,
            current_price=current, market_value=mv, cost_basis=abs(cost_basis or 0.0),
            unrealized_pl=upl, unrealized_pl_pct=upl_pct,
        ))
        if side == "long":
            n_long += 1
        else:
            n_short += 1
        if mv is not None:
            gross += abs(mv)
            net += mv
        total_cost += abs(cost_basis or 0.0)
        if upl is not None:
            total_upl += upl

    portfolio = Portfolio(
        equity=getattr(acct, "equity", None),
        cash=getattr(acct, "cash", None),
        daily_pl=getattr(acct, "daily_pl", None),
        n_open=len(positions), n_long=n_long, n_short=n_short,
        gross_exposure=gross, net_exposure=net,
        total_cost_basis=total_cost, total_unrealized_pl=total_upl,
        total_unrealized_pl_pct=(total_upl / total_cost) if total_cost > 0 else None,
    )
    # Sort biggest movers first (by |unrealized_pl|).
    positions.sort(key=lambda x: abs(x.unrealized_pl or 0.0), reverse=True)
    return PositionsResponse(positions=positions, portfolio=portfolio,
                             as_of=_now(), alpaca_ok=True)
