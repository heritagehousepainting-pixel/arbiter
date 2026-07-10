"""Symbol-level broker-rejection handling (2026-07-10 SPCX incident).

The engine tried to BUY ``SPCX`` — an untradable symbol its news advisor
picked up.  Alpaca rejected the order (non-200) and the engine treated the
per-ORDER rejection as broker-fatal, auto-pausing ALL trading.

The required behavior under test:

  * A symbol-level 4xx rejection (422 unprocessable / 404 asset-not-found /
    "asset not tradable") must skip THAT order only — no breaker trip, no
    BrokerError, no order row persisted — blacklist the ticker IN-MEMORY so
    it is not retried this session, and CONTINUE the cycle (other orders
    still submit; the engine is NOT auto-paused).
  * A genuinely SYSTEMIC broker failure (401 auth, 403 account, 5xx,
    timeouts/connectivity) keeps the existing broker-fatal auto-pause.

OFFLINE (spec §5): the broker is the in-memory ``FakeAlpaca`` wired into
``AlpacaAdapter`` through its injectable HTTP callables.  No network.
"""
from __future__ import annotations

import dataclasses
from datetime import timedelta
from pathlib import Path

import pytest

from arbiter.config import load_config
from arbiter.data.clock import BacktestClock
from arbiter.data.pit import FixtureSource, PITGateway
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.execution.alpaca_adapter import (
    AlpacaAdapter,
    BrokerError,
    is_symbol_rejection,
)
from arbiter.execution.submit import submit_order
from arbiter.safety.breakers import CircuitBreaker

from tests.execution._fake_alpaca import FakeAlpaca
from tests.execution.conftest import make_paper_order

# Reuse the end-to-end seeding helpers (same package-relative import style as
# test_alpaca_paper_mode.py).
from tests.integration.test_end_to_end import _AS_OF, _seed_cluster_buy


# ---------------------------------------------------------------------------
# Fake HTTP-status errors (mimic httpx.HTTPStatusError's .response seam)
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


class _HTTPStatusError(Exception):
    """Stand-in for httpx.HTTPStatusError: carries .response.status_code/.text."""

    def __init__(self, message: str, status_code: int, body: str = "") -> None:
        super().__init__(message)
        self.response = _FakeResponse(status_code, body)


def _invalid_symbol_422(symbol: str = "SPCX") -> _HTTPStatusError:
    return _HTTPStatusError(
        "Client error '422 Unprocessable Entity' for url "
        "'https://paper-api.alpaca.markets/v2/orders'",
        422,
        body=f'{{"code":40410000,"message":"asset {symbol} is not tradable"}}',
    )


def _auth_401() -> _HTTPStatusError:
    return _HTTPStatusError(
        "Client error '401 Unauthorized' for url "
        "'https://paper-api.alpaca.markets/v2/orders'",
        401,
        body='{"message":"access key verification failed"}',
    )


# ---------------------------------------------------------------------------
# Config helper (paper keys present, hermetic URLs)
# ---------------------------------------------------------------------------


def _adapter_cfg():
    return dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        kill_switch_url="",
        alert_webhook_url="",
    )


# ---------------------------------------------------------------------------
# 1. Classifier truth table
# ---------------------------------------------------------------------------


class TestIsSymbolRejection:
    @pytest.mark.parametrize("status", [404, 422])
    def test_symbol_level_status_codes(self, status: int) -> None:
        assert is_symbol_rejection("Broker non-200 after 2 attempts", status_code=status) is True

    @pytest.mark.parametrize("status", [400, 401, 403, 429, 500, 502, 503])
    def test_systemic_status_codes(self, status: int) -> None:
        assert is_symbol_rejection("Broker non-200 after 2 attempts", status_code=status) is False

    def test_httpx_style_422_message_without_code(self) -> None:
        """httpx embeds the code as \"error '422 ...'\" — parsed from the text."""
        msg = (
            "Broker non-200 after 2 attempts: Client error '422 Unprocessable "
            "Entity' for url 'https://paper-api.alpaca.markets/v2/orders'"
        )
        assert is_symbol_rejection(msg) is True

    def test_place_stamped_http_code_parsed(self) -> None:
        """place() stamps '[HTTP 422]' into reject_reason — parsed from the text."""
        assert is_symbol_rejection("[HTTP 422] Broker non-200 after 2 attempts: boom") is True
        assert is_symbol_rejection("[HTTP 503] Broker non-200 after 2 attempts: boom") is False

    def test_not_tradable_text_without_any_code(self) -> None:
        assert is_symbol_rejection('asset "SPCX" is not tradable') is True
        assert is_symbol_rejection("asset not found") is True

    def test_non_200_wording_is_not_mistaken_for_code_200(self) -> None:
        """The ubiquitous 'Broker non-200' phrasing must not parse as HTTP 200."""
        assert is_symbol_rejection("Broker non-200 after 2 attempts: HTTP 503") is False

    @pytest.mark.parametrize(
        "msg",
        [
            "",
            "connection timed out",
            "Broker non-200 after 2 attempts: read timed out",
            "no buying power",
        ],
    )
    def test_unclassifiable_defaults_to_systemic(self, msg: str) -> None:
        assert is_symbol_rejection(msg) is False

    def test_duplicate_client_order_id_422_stays_systemic(self) -> None:
        """A duplicate-client_order_id 422 (lost-response retry) is NOT a symbol
        problem — the first POST likely succeeded at the broker, so skipping
        would strand an untracked live order.  Must stay broker-fatal."""
        assert (
            is_symbol_rejection(
                '{"code":40010001,"message":"client_order_id must be unique"}',
                status_code=422,
            )
            is False
        )


# ---------------------------------------------------------------------------
# 2. Adapter stamps the HTTP status into the rejected report
# ---------------------------------------------------------------------------


class TestAdapterStampsStatusCode:
    def test_place_422_reject_reason_carries_http_code(self) -> None:
        def post_422(url, headers, json_body):
            raise _invalid_symbol_422()

        adapter = AlpacaAdapter(config=_adapter_cfg(), http_post=post_422)
        from arbiter.shared.executor import OrderIntent
        from arbiter.types import OrderSide

        report = adapter.place(
            OrderIntent("OID-1", "SPCX", OrderSide.BUY, qty=10.0, limit_price=5.0)
        )
        assert report.status == "rejected"
        assert "[HTTP 422]" in report.reject_reason
        # Existing contract preserved: attempts wording survives.
        assert "attempts" in report.reject_reason

    def test_place_plain_exception_reject_reason_has_no_stamp(self) -> None:
        def post_boom(url, headers, json_body):
            raise RuntimeError("HTTP 503")

        adapter = AlpacaAdapter(config=_adapter_cfg(), http_post=post_boom)
        from arbiter.shared.executor import OrderIntent
        from arbiter.types import OrderSide

        report = adapter.place(
            OrderIntent("OID-2", "AAPL", OrderSide.BUY, qty=10.0, limit_price=5.0)
        )
        assert report.status == "rejected"
        assert "[HTTP" not in report.reject_reason


# ---------------------------------------------------------------------------
# 3. submit_order: symbol-level 4xx skips; systemic still raises + trips
# ---------------------------------------------------------------------------


class TestSubmitOrderSymbolRejection:
    def test_symbol_422_skips_without_raise_breaker_or_persist(
        self, mem_conn, fixed_clock, tmp_audit
    ) -> None:
        def post_422(url, headers, json_body):
            raise _invalid_symbol_422()

        executor = AlpacaAdapter(config=_adapter_cfg(), http_post=post_422)
        breaker = CircuitBreaker()
        order = make_paper_order(ticker="SPCX", qty=5_000.0)

        result = submit_order(
            order,
            executor,
            fixed_clock,
            conn=mem_conn,
            spread=0.01,
            raw_price=5.0,
            breaker=breaker,
            audit_path=str(tmp_audit),
        )

        assert result.symbol_rejected is True
        assert result.order_id is None
        assert result.filled is False
        assert result.duplicate is False
        # NO breaker trip (a tripped breaker would gate the whole next cycle).
        assert breaker.any_tripped(mem_conn) == []
        # NO order row persisted (dedup slot stays free).
        row = mem_conn.execute(
            "SELECT 1 FROM orders WHERE dedup_hash = ?", (order.dedup_hash,)
        ).fetchone()
        assert row is None

    def test_systemic_503_still_raises_and_trips_breaker(
        self, mem_conn, fixed_clock, tmp_audit
    ) -> None:
        def post_503(url, headers, json_body):
            raise RuntimeError("HTTP 503")

        executor = AlpacaAdapter(config=_adapter_cfg(), http_post=post_503)
        breaker = CircuitBreaker()
        order = make_paper_order(qty=5_000.0)

        with pytest.raises(BrokerError):
            submit_order(
                order,
                executor,
                fixed_clock,
                conn=mem_conn,
                spread=0.01,
                raw_price=150.0,
                breaker=breaker,
                audit_path=str(tmp_audit),
            )
        assert "broker_non_200" in breaker.any_tripped(mem_conn)

    def test_systemic_401_auth_still_raises(
        self, mem_conn, fixed_clock, tmp_audit
    ) -> None:
        def post_401(url, headers, json_body):
            raise _auth_401()

        executor = AlpacaAdapter(config=_adapter_cfg(), http_post=post_401)
        breaker = CircuitBreaker()
        order = make_paper_order(qty=5_000.0)

        with pytest.raises(BrokerError):
            submit_order(
                order,
                executor,
                fixed_clock,
                conn=mem_conn,
                spread=0.01,
                raw_price=150.0,
                breaker=breaker,
                audit_path=str(tmp_audit),
            )
        assert "broker_non_200" in breaker.any_tripped(mem_conn)


# ---------------------------------------------------------------------------
# 4. Engine-level: skip + blacklist + continue vs systemic auto-pause
# ---------------------------------------------------------------------------


def _build_pit(tickers) -> PITGateway:
    """PITGateway seeded with price/spread/adv for each ticker (multi-ticker
    variant of tests.integration.test_end_to_end._build_pit_with_price)."""
    fixture = FixtureSource()
    ts_seed = _AS_OF - timedelta(days=1)
    for ticker in tickers:
        fixture.add("price_close", ticker, ts_seed, 150.0)
        fixture.add("price_open", ticker, ts_seed, 150.0)
        fixture.add("spread", ticker, ts_seed, 0.01)
        fixture.add("adv_20d", ticker, ts_seed, 10_000_000.0)
    pit = PITGateway()
    pit.register_source("price_close", fixture)
    pit.register_source("price_open", fixture)
    pit.register_source("spread", fixture)
    pit.register_source("adv_20d", fixture)
    return pit


def _build_paper_engine(tmp_path, monkeypatch, *, http_post, fake, tickers, audit: Path):
    """Engine whose executor is an AlpacaAdapter with an injected http_post."""
    db_path = str(tmp_path / "paper.db")
    config = dataclasses.replace(
        load_config(),
        live_trading=False,
        executor_backend="alpaca_paper",
        alpaca_api_key="key",
        alpaca_secret_key="secret",
        db_path=db_path,
        audit_path=str(audit),
        metrics_path=str(tmp_path / "metrics.jsonl"),
        # Hermetic: never inherit the real .env kill-switch / alert URLs.
        kill_switch_url="",
        alert_webhook_url="",
    )
    clock = BacktestClock(_AS_OF)
    conn = get_connection(db_path)
    run_migrations(conn, applied_at=_AS_OF.isoformat())
    for ticker in tickers:
        _seed_cluster_buy(conn, lambda: _AS_OF.isoformat(), ticker=ticker, n_buyers=3)
    pit = _build_pit(tickers)

    adapter = AlpacaAdapter(
        config=config,
        http_post=http_post,
        http_get=fake.http_get,
        http_delete=fake.http_delete,
    )
    monkeypatch.setattr("arbiter.engine.build_executor", lambda cfg: adapter)
    from arbiter.engine import build_engine

    eng = build_engine(config, conn=conn, pit=pit, clock=clock)
    return eng, conn


@pytest.fixture()
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


class TestEngineSymbolRejection:
    def test_symbol_rejection_skips_blacklists_and_continues(
        self, tmp_path, monkeypatch, audit_path
    ) -> None:
        """One bad symbol (SPCX, 422) must NOT pause the engine: the SPCX order
        is skipped, SPCX is blacklisted in-memory, and the OTHER ticker's order
        still submits in the SAME cycle."""
        fake = FakeAlpaca(cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)
        posted: list[str] = []

        def post(url, headers, json_body):
            if url.endswith("/v2/orders"):
                posted.append(json_body["symbol"])
                if json_body["symbol"] == "SPCX":
                    raise _invalid_symbol_422("SPCX")
            return fake.http_post(url, headers, json_body)

        eng, conn = _build_paper_engine(
            tmp_path, monkeypatch,
            http_post=post, fake=fake, tickers=("SPCX", "AAPL"), audit=audit_path,
        )

        result = eng.run_cycle(as_of=_AS_OF)

        # NOT auto-paused, breaker NOT tripped.
        assert eng.paused is False
        assert getattr(result, "paused_by_alert", False) is False
        assert "broker_non_200" not in eng.breaker.any_tripped(conn)

        # The cycle CONTINUED: the healthy ticker's order was placed + filled.
        row = conn.execute("SELECT status FROM orders WHERE ticker='AAPL'").fetchone()
        assert row is not None and row["status"] == "filled"
        # The rejected order was NOT persisted.
        assert conn.execute(
            "SELECT COUNT(*) c FROM orders WHERE ticker='SPCX'"
        ).fetchone()["c"] == 0

        # The symbol is blacklisted in-memory for the session.
        assert "SPCX" in eng._symbol_blacklist

        # NOT retried: a later cycle never re-POSTs SPCX to the broker.
        spcx_posts_after_first_cycle = posted.count("SPCX")
        assert spcx_posts_after_first_cycle >= 1
        eng.run_cycle(as_of=_AS_OF + timedelta(days=1))
        assert posted.count("SPCX") == spcx_posts_after_first_cycle
        assert eng.paused is False

    def test_blacklisted_symbol_never_reaches_broker(
        self, tmp_path, monkeypatch, audit_path
    ) -> None:
        """A pre-blacklisted symbol is skipped BEFORE any broker POST."""
        fake = FakeAlpaca(cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)
        posted: list[str] = []

        def post(url, headers, json_body):
            if url.endswith("/v2/orders"):
                posted.append(json_body["symbol"])
            return fake.http_post(url, headers, json_body)

        eng, conn = _build_paper_engine(
            tmp_path, monkeypatch,
            http_post=post, fake=fake, tickers=("AAPL",), audit=audit_path,
        )
        eng._symbol_blacklist.add("AAPL")

        eng.run_cycle(as_of=_AS_OF)

        assert posted == [], "blacklisted symbol must not be submitted to the broker"
        assert eng.paused is False
        assert conn.execute("SELECT COUNT(*) c FROM orders").fetchone()["c"] == 0

    @pytest.mark.parametrize(
        "exc_factory",
        [
            pytest.param(_auth_401, id="401-auth"),
            pytest.param(lambda: RuntimeError("HTTP 503"), id="503-server"),
            pytest.param(lambda: TimeoutError("read timed out"), id="timeout"),
        ],
    )
    def test_systemic_failure_still_auto_pauses(
        self, tmp_path, monkeypatch, audit_path, exc_factory
    ) -> None:
        """UNCHANGED safety: auth/5xx/timeout failures keep the broker-fatal
        auto-pause (a real broker outage must still pause)."""
        fake = FakeAlpaca(cash=1_000_000.0, equity=1_000_000.0, last_equity=1_000_000.0)

        def post(url, headers, json_body):
            raise exc_factory()

        eng, conn = _build_paper_engine(
            tmp_path, monkeypatch,
            http_post=post, fake=fake, tickers=("AAPL",), audit=audit_path,
        )

        eng.run_cycle(as_of=_AS_OF)

        assert eng.paused is True
        assert "broker_non_200" in eng.breaker.any_tripped(conn)
        # A systemic failure must NOT blacklist the symbol (it is not the
        # symbol's fault; after resume the order may be retried).
        assert "AAPL" not in eng._symbol_blacklist
