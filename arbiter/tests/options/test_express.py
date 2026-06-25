"""Integration tests for the options expression orchestrator (express.py).

The module agents are unit-tested separately; this covers the SPINE I own:
the off=no-op isolation guarantee, the shadow happy path (gate→select→size→
shadow row), gate reject, cross-cycle dedup, no-live-price inertness, and the
fail-safe (a client error must never propagate into the equity cycle).
"""
from __future__ import annotations

import datetime as _dt
from types import SimpleNamespace

from arbiter.config import Config
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.options.express import express_option
from arbiter.options.types import OptionContract, OptionSide
from arbiter.policy.book import RiskBook

_AS_OF = _dt.datetime(2026, 6, 25, 14, 0, 0, tzinfo=_dt.timezone.utc)


def _make_config(**overrides: object) -> Config:
    base: dict[str, object] = dict(
        live_trading=False, executor_backend="sim", db_path=":memory:",
        audit_path="/tmp/o_audit.jsonl", metrics_path="/tmp/o_metrics.jsonl",
        max_position_pct=0.05, max_sector_pct=0.20, max_gross_pct=0.80,
        max_open_positions=20, adv_cap_pct=0.02,
        alpaca_api_key="", alpaca_secret_key="",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets", alpaca_timeout=20.0,
        edgar_user_agent="", kill_switch_url="", alert_webhook_url="",
        options_mode="shadow", option_conviction_mult=1.5,
        option_min_expiry_days=60, option_ivr_max=0.40,
        option_target_delta_low=0.70, option_target_delta_high=0.80,
        option_min_open_interest=100, option_min_volume=10,
        options_sleeve_pct=0.35,
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


def _contract() -> OptionContract:
    return OptionContract(
        occ_symbol="AAPL261218C00230000", underlying="AAPL", side=OptionSide.CALL,
        strike=230.0, expiry=_dt.date(2026, 12, 18), delta=0.75, iv=0.34,
        bid=5.0, ask=5.2, open_interest=2000, volume=400,
    )


class _FakeClient:
    def __init__(self, *, chain=None, raise_on_chain=False):
        self._chain = chain if chain is not None else [_contract()]
        self._raise = raise_on_chain

    def fetch_chain(self, underlying, *, min_expiry, max_expiry, side, limit=100):
        if self._raise:
            raise RuntimeError("alpaca down")
        return list(self._chain)

    def snapshot(self, occ_symbols, *, feed="indicative"):
        return {}

    def place(self, order):
        raise NotImplementedError


class _FakePrice:
    def __init__(self, price):
        self._p = price

    def current_price(self, ticker):
        return self._p

    def current_prices(self, tickers):
        return {}


class _Clock:
    def now(self):
        return _AS_OF


def _conn():
    c = get_connection(":memory:")
    run_migrations(c)
    return c


def _idea(idea_id="01IDEA0000000000000000000A", ticker="AAPL", horizon=120):
    return SimpleNamespace(idea_id=idea_id, ticker=ticker, horizon_days=horizon)


def _fusion(conviction=0.20, catalyst="A1.activist"):
    return SimpleNamespace(
        conviction=conviction, advisor_contributions={catalyst: conviction}
    )


def _call(conn, *, config, client, price=230.0, book=None):
    return express_option(
        conn, _idea(), _fusion(),
        config=config, book_container=book if book is not None else [RiskBook({}, lambda t: "UNKNOWN")],
        clock=_Clock(),
        portfolio_equity=100_000.0, open_options_premium=0.0,
        current_price_provider=_FakePrice(price), client=client,
    )


def test_off_mode_is_total_noop():
    conn = _conn()
    out = _call(conn, config=_make_config(options_mode="off"), client=_FakeClient())
    assert out is None
    assert conn.execute("SELECT COUNT(*) FROM option_shadow_log").fetchone()[0] == 0


def test_shadow_happy_path_writes_row():
    conn = _conn()
    out = _call(conn, config=_make_config(), client=_FakeClient())
    assert out is not None
    rows = conn.execute(
        "SELECT occ_symbol, side, contracts_qty, delta_adjusted_notional, catalyst_tag "
        "FROM option_shadow_log"
    ).fetchall()
    assert len(rows) == 1
    r = rows[0]
    assert r["occ_symbol"] == "AAPL261218C00230000"
    assert r["side"] == "call"
    assert r["contracts_qty"] >= 1
    assert r["delta_adjusted_notional"] > 0
    assert r["catalyst_tag"] == "A1.activist"


def test_low_conviction_rejected_no_row():
    conn = _conn()
    out = express_option(
        conn, _idea(), _fusion(conviction=0.01),  # below 0.05*1.5
        config=_make_config(), book_container=[RiskBook({}, lambda t: "UNKNOWN")], clock=_Clock(),
        portfolio_equity=100_000.0, open_options_premium=0.0,
        current_price_provider=_FakePrice(230.0), client=_FakeClient(),
    )
    assert out is None
    assert conn.execute("SELECT COUNT(*) FROM option_shadow_log").fetchone()[0] == 0


def test_dedup_one_row_per_idea():
    conn = _conn()
    cfg = _make_config()
    first = _call(conn, config=cfg, client=_FakeClient())
    second = _call(conn, config=cfg, client=_FakeClient())
    assert first is not None and second is None
    assert conn.execute("SELECT COUNT(*) FROM option_shadow_log").fetchone()[0] == 1


def test_no_live_price_is_inert():
    conn = _conn()
    out = _call(conn, config=_make_config(), client=_FakeClient(), price=None)
    assert out is None
    assert conn.execute("SELECT COUNT(*) FROM option_shadow_log").fetchone()[0] == 0


def test_client_error_is_failsafe():
    conn = _conn()
    out = _call(conn, config=_make_config(), client=_FakeClient(raise_on_chain=True))
    assert out is None  # swallowed; equity path must never break
    assert conn.execute("SELECT COUNT(*) FROM option_shadow_log").fetchone()[0] == 0


# --- P2 paper branch ---------------------------------------------------------

class _FakePaperClient(_FakeClient):
    def __init__(self, *, place_fail=False, **kw):
        super().__init__(**kw)
        self.placed = []
        self._place_fail = place_fail

    def place(self, order):
        if self._place_fail:
            from arbiter.options.alpaca_options_client import OptionsBrokerError
            raise OptionsBrokerError("broker 422")
        self.placed.append(order)
        return {"id": "brk-1", "status": "accepted"}


def test_paper_places_records_position_and_folds_delta():
    conn = _conn()
    book = [RiskBook({}, lambda t: "UNKNOWN")]
    client = _FakePaperClient()
    out = _call(conn, config=_make_config(options_mode="paper"), client=client, book=book)
    assert out is not None
    assert len(client.placed) == 1  # real (paper) order placed
    pos = conn.execute(
        "SELECT idea_id, occ_symbol, broker_order_id, contracts_qty FROM option_positions"
    ).fetchall()
    assert len(pos) == 1
    assert pos[0]["broker_order_id"] == "brk-1"
    # No shadow row in paper mode; delta folded into the live book.
    assert conn.execute("SELECT COUNT(*) FROM option_shadow_log").fetchone()[0] == 0
    assert book[0].gross_exposure() > 0


def test_paper_place_failure_does_not_open_or_fold():
    conn = _conn()
    book = [RiskBook({}, lambda t: "UNKNOWN")]
    client = _FakePaperClient(place_fail=True)
    out = _call(conn, config=_make_config(options_mode="paper"), client=client, book=book)
    assert out is None  # swallowed
    assert conn.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0] == 0
    assert book[0].gross_exposure() == 0  # no budget consumed on failed placement


def test_paper_dedup_one_open_position_per_idea():
    conn = _conn()
    cfg = _make_config(options_mode="paper")
    first = _call(conn, config=cfg, client=_FakePaperClient())
    second = _call(conn, config=cfg, client=_FakePaperClient())
    assert first is not None and second is None
    assert conn.execute("SELECT COUNT(*) FROM option_positions").fetchone()[0] == 1
