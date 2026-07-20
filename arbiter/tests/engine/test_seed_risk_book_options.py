"""Tier-2 #7 — options delta-notional folds into the cycle-start RiskBook.

Spec context: a standing option position (UBER $65C Jun-2027, ~$5.7k
delta-notional) was invisible to the sector/gross/name caps when sizing
equity entries, because ``seed_risk_book`` only read ``executor.get_positions()``
(equities).  The fold uses the at-open snapshot — the same exposure currency
``express.py`` folds on a live option buy.  A CLOSED position (matching
``option_outcomes`` row) must NOT fold.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from arbiter.db.connection import get_connection
from arbiter.db.helpers import generate_ulid, insert_row
from arbiter.db.migrate import run_migrations
from arbiter.engine.safety_ops import _option_delta_notional, seed_risk_book

_AS_OF = datetime(2025, 3, 17, 14, 0, 0, tzinfo=timezone.utc)


class _EmptyExecutor:
    name = "sim"

    def get_positions(self):
        return {}


def _conn(tmp_path):
    conn = get_connection(str(tmp_path / "t.db"))
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    return conn


def _engine_stub(conn):
    return SimpleNamespace(executor=_EmptyExecutor(), conn=conn)


def _seed_option(conn, *, underlying="UBER", delta=0.7508, qty=1,
                 und_price=76.355, idea_id=None, occ=None):
    idea_id = idea_id or generate_ulid()
    occ = occ or f"{underlying}270617C00065000"
    insert_row(conn, "option_positions", {
        "id": generate_ulid(),
        "idea_id": idea_id,
        "shadow_id": None,
        "underlying": underlying,
        "occ_symbol": occ,
        "side": "call",
        "strike": 65.0,
        "expiry": "2027-06-17",
        "contracts_qty": qty,
        "entry_premium": 1998.5,
        "entry_limit_price": 19.985,
        "delta_at_open": delta,
        "iv_at_open": 0.4374,
        "underlying_open_price": und_price,
        "thesis_horizon_date": "2026-12-23",
        "original_conviction": 0.43,
        "broker_order_id": "b-1",
        "open_ts": _AS_OF.isoformat(),
        "created_at": _AS_OF.isoformat(),
    })
    return idea_id, occ


def test_open_option_folds_delta_notional_into_name_overlay_only(tmp_path):
    """Two-working-books (2026-07-20): an open option guards the PER-NAME cap
    for its underlying (cross-book anti-doubling) but no longer counts toward
    the equity gross budget — a standing LEAPS cannot freeze the stock book."""
    conn = _conn(tmp_path)
    _seed_option(conn)
    book = seed_risk_book(_engine_stub(conn), _AS_OF)
    expected = 0.7508 * 100 * 1 * 76.355  # ≈ $5,732.73
    assert book.name_exposure_for("UBER") == abs(expected)
    assert book.gross_exposure() == 0.0  # equity book untouched
    assert book.open_positions() == 0    # options don't consume equity slots


def test_closed_option_does_not_fold(tmp_path):
    conn = _conn(tmp_path)
    idea_id, occ = _seed_option(conn)
    # A close = an option_outcomes row matching (idea_id, occ_symbol).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(option_outcomes)")}
    row = {c: None for c in cols}
    row.update({
        "id": generate_ulid(), "idea_id": idea_id, "occ_symbol": occ,
    })
    # Fill NOT NULL columns generically so the insert is schema-agnostic.
    for r in conn.execute("PRAGMA table_info(option_outcomes)").fetchall():
        name, notnull = r[1], bool(r[3])
        if notnull and row.get(name) is None:
            coltype = str(r[2]).upper()
            row[name] = 0 if ("INT" in coltype or "REAL" in coltype) else "x"
    insert_row(conn, "option_outcomes", row)

    book = seed_risk_book(_engine_stub(conn), _AS_OF)
    assert book.name_exposure_for("UBER") == 0.0


def test_malformed_delta_row_is_skipped(tmp_path):
    conn = _conn(tmp_path)
    _seed_option(conn, delta=None)  # delta_at_open nullable → malformed for fold
    book = seed_risk_book(_engine_stub(conn), _AS_OF)
    assert book.gross_exposure() == 0.0


def test_delta_notional_math():
    assert _option_delta_notional(
        {"delta_at_open": -0.5, "contracts_qty": 2, "underlying_open_price": 100.0}
    ) == 10_000.0  # |delta| — a put's exposure folds as magnitude
    assert _option_delta_notional({"delta_at_open": None, "contracts_qty": 1,
                                   "underlying_open_price": 100.0}) == 0.0
    assert _option_delta_notional({}) == 0.0
