"""Read-only options data queries for the cockpit.

Implements ``build_options_state()`` and ``build_iv_series()`` — mirrors the
pattern of ``positions.py``.  All SQL is read-only SELECT against the four
option tables: option_positions, option_shadow_log, option_outcomes,
option_iv_history.

Openness rule (mirrors arbiter/options/positions.py exactly):
  An open position = option_positions row with NO matching option_outcomes row
  on (idea_id, occ_symbol).
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone


from .contract import (
    IVPoint,
    IVSeries,
    OpenOptionPosition,
    OptionOutcomeRecord,
    OptionShadowPlay,
    OptionsMode,
    OptionsState,
)
from .db import DEFAULT_DB_PATH

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # <repo>/arbiter


def _fetch_option_mids(occ_symbols: list[str]) -> dict[str, float]:
    """Best-effort current mid per OCC symbol via the FREE indicative options feed.

    Mirrors arbiter/options/manage.py: mid = (bid+ask)/2 (else bid or ask).  This
    is an *indicative* mark (wide spreads), not a guaranteed fill.  Returns {} on
    any failure so the panel degrades to "—".  Read-only (snapshot GET only).
    """
    if not occ_symbols:
        return {}
    try:
        import sys  # noqa: PLC0415
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.options.alpaca_options_client import AlpacaOptionsClient  # noqa: PLC0415

        client = AlpacaOptionsClient(load_config())
        snap_map = client.snapshot(occ_symbols)
    except Exception:
        return {}

    mids: dict[str, float] = {}
    for occ, snap in (snap_map or {}).items():
        if not isinstance(snap, dict):
            continue
        bid, ask = snap.get("bid"), snap.get("ask")
        mid: float | None = None
        if bid and ask:
            mid = (float(bid) + float(ask)) / 2.0
        elif bid:
            mid = float(bid)
        elif ask:
            mid = float(ask)
        if mid is not None and mid > 0:
            mids[str(occ)] = mid
    return mids


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _options_mode_from_dotenv() -> str | None:
    """Read OPTIONS_MODE from the arbiter ``.env`` (read-only), or None.

    The cockpit runs as a SEPARATE process that does not load ``arbiter/.env``,
    so ``os.environ`` alone is blind to the mode the daemon actually runs in.
    The arbiter dir is the parent of the DB dir (``arbiter/data/arbiter.db`` →
    ``arbiter/.env``) — read it directly so the cockpit reflects reality.
    """
    from .db import db_path  # noqa: PLC0415

    try:
        env_path = db_path().resolve().parents[1] / ".env"
        for raw in env_path.read_text().splitlines():
            line = raw.strip()
            if line.startswith("OPTIONS_MODE=") and not line.startswith("#"):
                val = line.split("=", 1)[1].split("#", 1)[0].strip()
                return val or None
    except (OSError, IndexError):
        return None
    return None


def _options_mode() -> OptionsMode:
    """Effective OPTIONS_MODE: os.environ first, then the arbiter ``.env``.

    Mirrors the daemon's precedence (a real env var wins over the .env file)
    while still surfacing the .env value the daemon loaded — which the cockpit
    process never loads itself.
    """
    mode = (os.environ.get("OPTIONS_MODE") or _options_mode_from_dotenv() or "off").lower()
    if mode not in ("off", "shadow", "paper"):
        return "off"
    return mode  # type: ignore[return-value]


def _list_open_positions(conn: sqlite3.Connection) -> list[OpenOptionPosition]:
    """Return open option positions via the LEFT JOIN absence pattern."""
    today = _utcnow().date()
    try:
        rows = conn.execute(
            """
            SELECT p.id, p.idea_id, p.underlying, p.occ_symbol, p.side,
                   p.strike, p.expiry, p.contracts_qty, p.entry_premium,
                   p.delta_at_open, p.iv_at_open, p.underlying_open_price,
                   p.thesis_horizon_date, p.original_conviction, p.open_ts
            FROM option_positions AS p
            LEFT JOIN option_outcomes AS o
                ON o.idea_id    = p.idea_id
               AND o.occ_symbol = p.occ_symbol
            WHERE o.id IS NULL
            ORDER BY p.open_ts
            """
        ).fetchall()
    except Exception:
        return []

    # Live (indicative) mark per contract → real ROI $ and %.
    mids = _fetch_option_mids([str(r["occ_symbol"]) for r in rows])

    positions: list[OpenOptionPosition] = []
    for r in rows:
        expiry_str = str(r["expiry"])
        dte: int | None = None
        try:
            expiry_date = datetime.fromisoformat(expiry_str).date()
            dte = (expiry_date - today).days
        except Exception:
            pass

        qty = int(r["contracts_qty"])
        entry = float(r["entry_premium"])
        mid = mids.get(str(r["occ_symbol"]))
        unrealized_pl: float | None = None
        unrealized_pl_pct: float | None = None
        if mid is not None and mid > 0:
            current_value = mid * 100.0 * qty   # option premium is per-share ×100
            unrealized_pl = current_value - entry
            unrealized_pl_pct = (unrealized_pl / entry) if entry else None

        positions.append(OpenOptionPosition(
            id=str(r["id"]),
            idea_id=str(r["idea_id"]),
            underlying=str(r["underlying"]),
            occ_symbol=str(r["occ_symbol"]),
            side=str(r["side"]),
            strike=float(r["strike"]),
            expiry=expiry_str,
            contracts_qty=int(r["contracts_qty"]),
            entry_premium=float(r["entry_premium"]),
            delta_at_open=float(r["delta_at_open"]) if r["delta_at_open"] is not None else None,
            iv_at_open=float(r["iv_at_open"]) if r["iv_at_open"] is not None else None,
            underlying_open_price=float(r["underlying_open_price"]),
            thesis_horizon_date=str(r["thesis_horizon_date"]),
            original_conviction=float(r["original_conviction"]),
            open_ts=str(r["open_ts"]),
            dte=dte,
            current_mid=mid,
            unrealized_pl=unrealized_pl,
            unrealized_pl_pct=unrealized_pl_pct,
        ))
    return positions


def _list_recent_shadow_plays(conn: sqlite3.Connection, limit: int = 20) -> list[OptionShadowPlay]:
    try:
        rows = conn.execute(
            """
            SELECT id, idea_id, underlying, as_of,
                   gate_express, gate_reason, side, occ_symbol,
                   strike, expiry, delta, iv,
                   est_premium, delta_adjusted_notional, contracts_qty,
                   conviction, horizon_days, catalyst_tag, ivr_estimate, created_at
            FROM option_shadow_log
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        return []

    return [
        OptionShadowPlay(
            id=str(r["id"]),
            idea_id=str(r["idea_id"]),
            underlying=str(r["underlying"]),
            as_of=str(r["as_of"]),
            gate_express=bool(r["gate_express"]),
            gate_reason=str(r["gate_reason"]),
            side=str(r["side"]) if r["side"] is not None else None,
            occ_symbol=str(r["occ_symbol"]) if r["occ_symbol"] is not None else None,
            strike=float(r["strike"]) if r["strike"] is not None else None,
            expiry=str(r["expiry"]) if r["expiry"] is not None else None,
            delta=float(r["delta"]) if r["delta"] is not None else None,
            iv=float(r["iv"]) if r["iv"] is not None else None,
            est_premium=float(r["est_premium"]) if r["est_premium"] is not None else None,
            delta_adjusted_notional=(
                float(r["delta_adjusted_notional"])
                if r["delta_adjusted_notional"] is not None else None
            ),
            contracts_qty=int(r["contracts_qty"]) if r["contracts_qty"] is not None else None,
            conviction=float(r["conviction"]),
            horizon_days=float(r["horizon_days"]),
            catalyst_tag=str(r["catalyst_tag"]) if r["catalyst_tag"] is not None else None,
            ivr_estimate=float(r["ivr_estimate"]) if r["ivr_estimate"] is not None else None,
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def _list_recent_outcomes(conn: sqlite3.Connection, limit: int = 20) -> list[OptionOutcomeRecord]:
    try:
        rows = conn.execute(
            """
            SELECT id, idea_id, underlying, occ_symbol, side,
                   open_ts, close_ts, close_reason,
                   entry_premium, exit_premium, option_pl_pct,
                   underlying_alpha_bps,
                   delta_at_open, iv_at_open, iv_at_close,
                   contracts_qty, created_at
            FROM option_outcomes
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    except Exception:
        return []

    return [
        OptionOutcomeRecord(
            id=str(r["id"]),
            idea_id=str(r["idea_id"]),
            underlying=str(r["underlying"]),
            occ_symbol=str(r["occ_symbol"]),
            side=str(r["side"]),
            open_ts=str(r["open_ts"]),
            close_ts=str(r["close_ts"]),
            close_reason=str(r["close_reason"]),
            entry_premium=float(r["entry_premium"]),
            exit_premium=float(r["exit_premium"]),
            option_pl_pct=float(r["option_pl_pct"]),
            underlying_alpha_bps=float(r["underlying_alpha_bps"]),
            delta_at_open=float(r["delta_at_open"]) if r["delta_at_open"] is not None else None,
            iv_at_open=float(r["iv_at_open"]) if r["iv_at_open"] is not None else None,
            iv_at_close=float(r["iv_at_close"]) if r["iv_at_close"] is not None else None,
            contracts_qty=int(r["contracts_qty"]),
            created_at=str(r["created_at"]),
        )
        for r in rows
    ]


def _compute_aggregates(
    outcomes: list[OptionOutcomeRecord],
) -> tuple[float | None, float | None, float | None]:
    """Return (win_rate, avg_option_pl_pct, avg_underlying_alpha_bps) or (None, None, None)."""
    # Use all outcomes from the table (not just the recent 20) for accurate aggregates.
    # Caller may pass empty list when table is empty.
    if not outcomes:
        return None, None, None
    wins = sum(1 for o in outcomes if o.option_pl_pct > 0)
    win_rate = wins / len(outcomes)
    avg_pl = sum(o.option_pl_pct for o in outcomes) / len(outcomes)
    avg_alpha = sum(o.underlying_alpha_bps for o in outcomes) / len(outcomes)
    return win_rate, avg_pl, avg_alpha


def build_options_state(conn: sqlite3.Connection) -> OptionsState:
    """Build the complete OptionsState snapshot.  Degrades gracefully to empty."""
    options_mode = _options_mode()

    open_positions = _list_open_positions(conn)
    recent_shadows = _list_recent_shadow_plays(conn)

    # Fetch ALL outcomes for aggregate calculation (use recent_outcomes list too)
    recent_outcomes = _list_recent_outcomes(conn)

    # Aggregate stats over all closed outcomes (not just the 20 we surface)
    try:
        all_agg = conn.execute(
            """
            SELECT
                COUNT(*) AS n,
                SUM(CASE WHEN option_pl_pct > 0 THEN 1 ELSE 0 END) AS wins,
                AVG(option_pl_pct) AS avg_pl_pct,
                AVG(underlying_alpha_bps) AS avg_alpha_bps
            FROM option_outcomes
            """
        ).fetchone()
        if all_agg and int(all_agg["n"]) > 0:
            n_total = int(all_agg["n"])
            win_rate: float | None = float(all_agg["wins"]) / n_total
            avg_option_pl_pct: float | None = float(all_agg["avg_pl_pct"])
            avg_alpha_bps: float | None = float(all_agg["avg_alpha_bps"])
        else:
            win_rate = avg_option_pl_pct = avg_alpha_bps = None
    except Exception:
        win_rate = avg_option_pl_pct = avg_alpha_bps = None

    return OptionsState(
        options_mode=options_mode,
        open_positions=open_positions,
        recent_shadow_plays=recent_shadows,
        recent_outcomes=recent_outcomes,
        n_open=len(open_positions),
        sleeve_used_pct=None,   # requires Alpaca account equity — not from DB alone
        win_rate=win_rate,
        avg_option_pl_pct=avg_option_pl_pct,
        avg_underlying_alpha_bps=avg_alpha_bps,
        as_of=_now(),
    )


def build_iv_series(conn: sqlite3.Connection, ticker: str) -> IVSeries:
    """Return the ATM-IV history for *ticker*.  Never raises; empty is valid."""
    now = _now()
    ticker = ticker.upper()

    try:
        rows = conn.execute(
            """
            SELECT as_of, atm_iv, occ_symbol
            FROM option_iv_history
            WHERE underlying = ?
              AND as_of >= datetime('now', '-365 days')
            ORDER BY as_of ASC
            LIMIT 365
            """,
            (ticker,),
        ).fetchall()
    except Exception:
        return IVSeries(underlying=ticker, points=[], current_iv_rank=None, as_of=now)

    points = [
        IVPoint(as_of=str(r["as_of"]), atm_iv=float(r["atm_iv"]), occ_symbol=str(r["occ_symbol"]))
        for r in rows
    ]

    # IVR: fraction of period where current ATM IV > historical ATM IV
    # Requires >= 30 data points; mirrors arbiter iv_history.iv_rank()
    current_iv_rank: float | None = None
    if len(points) >= 30:
        current_iv = points[-1].atm_iv
        current_iv_rank = sum(1 for p in points if p.atm_iv < current_iv) / len(points)

    return IVSeries(
        underlying=ticker,
        points=points,
        current_iv_rank=current_iv_rank,
        as_of=now,
    )
