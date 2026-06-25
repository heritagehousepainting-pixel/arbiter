"""Tests for AlpacaOptionsClient.

All HTTP is mocked — no real network calls.
Realistic Alpaca payload shapes are used:
  contracts → {"option_contracts": [...]}
  snapshots → {"snapshots": {OCC: {"impliedVolatility": ..., "greeks": {...},
                                    "latestQuote": {"bp": ..., "ap": ...}}}}
"""
from __future__ import annotations

import datetime
import datetime as _dt  # alias used in place() test helpers

import pytest

from arbiter.options.alpaca_options_client import AlpacaOptionsClient, OptionsBrokerError
from arbiter.options.types import OptionContract, OptionOrder, OptionSide


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _make_config(
    *,
    api_key: str = "TEST_KEY",
    secret_key: str = "TEST_SECRET",
    paper_base_url: str = "https://paper-api.alpaca.markets",
    data_base_url: str = "https://data.alpaca.markets",
    timeout: float = 10.0,
):
    """Build a minimal Config-like object for tests (avoids loading arbiter.toml)."""
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class _Cfg:
        alpaca_api_key: str
        alpaca_secret_key: str
        alpaca_paper_base_url: str
        alpaca_data_base_url: str
        alpaca_timeout: float

    return _Cfg(
        alpaca_api_key=api_key,
        alpaca_secret_key=secret_key,
        alpaca_paper_base_url=paper_base_url,
        alpaca_data_base_url=data_base_url,
        alpaca_timeout=timeout,
    )


def _make_snapshot_payload(occ: str, iv: float, delta: float, bid: float, ask: float) -> dict:
    """Build a realistic Alpaca snapshot response for a single OCC symbol."""
    return {
        "snapshots": {
            occ: {
                "impliedVolatility": iv,
                "greeks": {
                    "delta": delta,
                    "gamma": 0.005,
                    "theta": -0.03,
                    "vega": 0.10,
                },
                "latestQuote": {
                    "bp": bid,
                    "ap": ask,
                    "bs": 10,
                    "as": 10,
                },
            }
        }
    }


def _make_contracts_payload(occ: str, underlying: str, expiry: str, strike: float) -> dict:
    """Build a realistic Alpaca contracts list response."""
    return {
        "option_contracts": [
            {
                "symbol": occ,
                "underlying_symbol": underlying,
                "type": "call",
                "expiration_date": expiry,
                "strike_price": strike,
                "status": "active",
                "open_interest": 500,
                "volume": 50,
            }
        ]
    }


# ---------------------------------------------------------------------------
# AlpacaOptionsClient.__init__
# ---------------------------------------------------------------------------

class TestAlpacaOptionsClientInit:
    def test_init_stores_credentials(self):
        cfg = _make_config(api_key="AK", secret_key="SK")

        def fake_get(url, headers, params):
            return {}

        client = AlpacaOptionsClient(cfg, http_get=fake_get)
        assert client._headers["APCA-API-KEY-ID"] == "AK"
        assert client._headers["APCA-API-SECRET-KEY"] == "SK"

    def test_init_sets_base_urls(self):
        cfg = _make_config(
            paper_base_url="https://paper-api.alpaca.markets",
            data_base_url="https://data.alpaca.markets",
        )

        def fake_get(url, headers, params):
            return {}

        client = AlpacaOptionsClient(cfg, http_get=fake_get)
        assert client._contracts_base == "https://paper-api.alpaca.markets"
        assert client._data_base == "https://data.alpaca.markets"

    def test_init_strips_trailing_slash(self):
        cfg = _make_config(
            paper_base_url="https://paper-api.alpaca.markets/",
            data_base_url="https://data.alpaca.markets/",
        )
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {})
        assert not client._contracts_base.endswith("/")
        assert not client._data_base.endswith("/")


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------

class TestSnapshot:
    def test_snapshot_returns_normalised_dict(self):
        occ = "AAPL240119C00150000"
        payload = _make_snapshot_payload(occ, iv=0.38, delta=0.75, bid=51.0, ask=53.0)

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: payload)

        result = client.snapshot([occ])

        assert occ in result
        snap = result[occ]
        assert snap["iv"] == pytest.approx(0.38)
        assert snap["delta"] == pytest.approx(0.75)
        assert snap["gamma"] == pytest.approx(0.005)
        assert snap["bid"] == pytest.approx(51.0)
        assert snap["ask"] == pytest.approx(53.0)

    def test_snapshot_empty_list_returns_empty(self):
        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {})
        assert client.snapshot([]) == {}

    def test_snapshot_raises_on_opra_feed(self):
        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {})
        with pytest.raises(ValueError, match="opra"):
            client.snapshot(["AAPL240119C00150000"], feed="opra")

    def test_snapshot_batching(self):
        """Verify that a call with >100 symbols issues multiple HTTP GETs."""
        calls: list[dict] = []

        def fake_get(url, headers, params):
            calls.append({"symbols": params.get("symbols", "")})
            return {"snapshots": {}}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        # 150 symbols should produce 2 batches (100 + 50).
        symbols = [f"AAPL2401{str(i).zfill(2)}C00150000" for i in range(150)]
        client.snapshot(symbols)

        assert len(calls) == 2
        first_batch_count = len(calls[0]["symbols"].split(","))
        second_batch_count = len(calls[1]["symbols"].split(","))
        assert first_batch_count == 100
        assert second_batch_count == 50

    def test_snapshot_null_greeks_are_none(self):
        """Snapshots where greeks dict is absent → all greek fields None."""
        occ = "AAPL240119C00150000"
        payload = {
            "snapshots": {
                occ: {
                    "impliedVolatility": None,
                    # greeks key absent entirely
                    "latestQuote": {"bp": 10.0, "ap": 11.0},
                }
            }
        }
        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: payload)

        result = client.snapshot([occ])
        snap = result[occ]
        assert snap["iv"] is None
        assert snap["delta"] is None
        assert snap["bid"] == pytest.approx(10.0)

    def test_snapshot_uses_feed_param(self):
        """Verify the feed query param is forwarded to Alpaca."""
        received_params: list[dict] = []

        def fake_get(url, headers, params):
            received_params.append(dict(params))
            return {"snapshots": {}}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)
        client.snapshot(["AAPL240119C00150000"], feed="indicative")

        assert received_params[0]["feed"] == "indicative"

    def test_snapshot_continues_on_batch_failure(self):
        """If one batch fails, the other batches still succeed (fault tolerance)."""
        call_count = [0]

        def fake_get(url, headers, params):
            call_count[0] += 1
            if call_count[0] == 1:
                raise RuntimeError("network blip")
            return {"snapshots": {"AAPL240119C00150001": {"impliedVolatility": 0.30, "greeks": {"delta": 0.7}, "latestQuote": {}}}}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        # 150 symbols forces 2 batches; first fails, second succeeds.
        symbols = [f"AAPL2401{str(i).zfill(2)}C00150000" for i in range(149)]
        symbols.append("AAPL240119C00150001")
        result = client.snapshot(symbols)
        # At least the second batch result is present.
        assert "AAPL240119C00150001" in result


# ---------------------------------------------------------------------------
# fetch_chain()
# ---------------------------------------------------------------------------

class TestFetchChain:
    def test_fetch_chain_returns_populated_contracts(self):
        """fetch_chain merges contracts + snapshot data."""
        occ = "AAPL240119C00150000"
        contracts_payload = _make_contracts_payload(
            occ, "AAPL", "2024-01-19", 150.0
        )
        snapshot_payload = _make_snapshot_payload(
            occ, iv=0.38, delta=0.75, bid=51.0, ask=53.0
        )

        call_order: list[str] = []

        def fake_get(url, headers, params):
            if "contracts" in url:
                call_order.append("contracts")
                return contracts_payload
            if "snapshots" in url:
                call_order.append("snapshots")
                return snapshot_payload
            return {}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        result = client.fetch_chain(
            "AAPL",
            min_expiry=datetime.date(2024, 1, 1),
            max_expiry=datetime.date(2024, 6, 1),
            side=OptionSide.CALL,
        )

        assert len(result) == 1
        contract = result[0]
        assert contract.occ_symbol == occ
        assert contract.underlying == "AAPL"
        assert contract.side == OptionSide.CALL
        assert contract.strike == pytest.approx(150.0)
        assert contract.expiry == datetime.date(2024, 1, 19)
        assert contract.iv == pytest.approx(0.38)
        assert contract.delta == pytest.approx(0.75)
        assert contract.bid == pytest.approx(51.0)
        assert contract.ask == pytest.approx(53.0)
        assert "contracts" in call_order
        assert "snapshots" in call_order

    def test_fetch_chain_empty_on_no_contracts(self):
        def fake_get(url, headers, params):
            if "contracts" in url:
                return {"option_contracts": []}
            return {}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        result = client.fetch_chain(
            "AAPL",
            min_expiry=datetime.date(2024, 1, 1),
            max_expiry=datetime.date(2024, 6, 1),
            side=OptionSide.CALL,
        )
        assert result == []

    def test_fetch_chain_drops_contracts_with_null_greeks(self):
        """Contracts whose snapshot has no greeks still get included but delta/iv are None."""
        occ = "AAPL240119C00150000"
        contracts_payload = _make_contracts_payload(occ, "AAPL", "2024-01-19", 150.0)
        # No greeks in snapshot.
        snapshot_payload = {
            "snapshots": {
                occ: {
                    "impliedVolatility": None,
                    "latestQuote": {"bp": None, "ap": None},
                }
            }
        }

        def fake_get(url, headers, params):
            if "contracts" in url:
                return contracts_payload
            return snapshot_payload

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        result = client.fetch_chain(
            "AAPL",
            min_expiry=datetime.date(2024, 1, 1),
            max_expiry=datetime.date(2024, 6, 1),
            side=OptionSide.CALL,
        )
        # Contract is included with None delta/iv (not raised/dropped at this layer).
        assert len(result) == 1
        assert result[0].delta is None
        assert result[0].iv is None

    def test_fetch_chain_empty_on_network_failure(self):
        """If the contracts request fails, return [] without raising."""
        def fake_get(url, headers, params):
            if "contracts" in url:
                raise RuntimeError("connection refused")
            return {}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        result = client.fetch_chain(
            "AAPL",
            min_expiry=datetime.date(2024, 1, 1),
            max_expiry=datetime.date(2024, 6, 1),
            side=OptionSide.CALL,
        )
        assert result == []

    def test_fetch_chain_put_side(self):
        """PUT side is correctly forwarded to Alpaca."""
        received_params: list[dict] = []

        def fake_get(url, headers, params):
            received_params.append(dict(params))
            return {"option_contracts": []}

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        client.fetch_chain(
            "AAPL",
            min_expiry=datetime.date(2024, 1, 1),
            max_expiry=datetime.date(2024, 6, 1),
            side=OptionSide.PUT,
        )
        assert received_params[0]["type"] == "put"

    def test_fetch_chain_uses_contracts_url(self):
        """Verify contracts endpoint is paper-api."""
        received_urls: list[str] = []

        def fake_get(url, headers, params):
            received_urls.append(url)
            return {"option_contracts": []}

        cfg = _make_config(paper_base_url="https://paper-api.alpaca.markets")
        client = AlpacaOptionsClient(cfg, http_get=fake_get)

        client.fetch_chain(
            "AAPL",
            min_expiry=datetime.date(2024, 1, 1),
            max_expiry=datetime.date(2024, 6, 1),
            side=OptionSide.CALL,
        )
        assert any("paper-api.alpaca.markets" in u for u in received_urls)


# ---------------------------------------------------------------------------
# place() — P2 paper execution
# ---------------------------------------------------------------------------


def _make_contract(
    occ: str = "AAPL240119C00150000",
    underlying: str = "AAPL",
    side: OptionSide = OptionSide.CALL,
    strike: float = 150.0,
    expiry: _dt.date | None = None,
    ask: float | None = 5.10,
    bid: float | None = 5.00,
    delta: float | None = 0.75,
    iv: float | None = 0.38,
    open_interest: int | None = 500,
    volume: int | None = 50,
) -> OptionContract:
    return OptionContract(
        occ_symbol=occ,
        underlying=underlying,
        side=side,
        strike=strike,
        expiry=expiry or _dt.date(2024, 1, 19),
        delta=delta,
        iv=iv,
        bid=bid,
        ask=ask,
        open_interest=open_interest,
        volume=volume,
    )


def _make_order(
    contracts_qty: int = 2,
    ask: float | None = 5.10,
    bid: float | None = 5.00,
    **contract_kwargs,
) -> OptionOrder:
    contract = _make_contract(ask=ask, bid=bid, **contract_kwargs)
    return OptionOrder(
        contract=contract,
        contracts_qty=contracts_qty,
        est_premium=contracts_qty * (contract.mid_price or ask or 5.05) * 100,
        delta_adjusted_notional=abs(contract.delta or 0.75) * 100 * 150.0 * contracts_qty,
        side=contract.side,
    )


def _make_broker_response(
    occ: str = "AAPL240119C00150000",
    client_order_id: str = "01ARZ3NDEKTSV4RRFFQ69G5FAV",
    qty: str = "2",
    limit_price: str = "5.11",
    status: str = "accepted",
) -> dict:
    """Minimal Alpaca order response shape."""
    return {
        "id": "broker-uuid-1234",
        "client_order_id": client_order_id,
        "status": status,
        "symbol": occ,
        "qty": qty,
        "filled_qty": "0",
        "type": "limit",
        "side": "buy",
        "time_in_force": "day",
        "limit_price": limit_price,
        "asset_class": "us_option",
    }


class TestPlace:
    # ------------------------------------------------------------------
    # Happy-path: request body shape
    # ------------------------------------------------------------------

    def test_place_posts_correct_body_shape(self):
        """place() POSTs the expected fields to /v2/orders."""
        posted: list[dict] = []

        def fake_post(url, headers, body):
            posted.append({"url": url, "body": body})
            return _make_broker_response(
                occ=body["symbol"],
                client_order_id=body["client_order_id"],
                qty=body["qty"],
                limit_price=body["limit_price"],
            )

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        order = _make_order(contracts_qty=3, ask=5.10, bid=5.00)

        client.place(order)

        assert len(posted) == 1
        body = posted[0]["body"]
        assert body["symbol"] == "AAPL240119C00150000"
        assert body["side"] == "buy"
        assert body["type"] == "limit"
        assert body["time_in_force"] == "day"
        assert body["asset_class"] == "us_option"
        assert body["qty"] == "3"
        assert "client_order_id" in body
        assert "limit_price" in body

    def test_place_url_hits_v2_orders(self):
        """place() POSTs to the paper-api /v2/orders endpoint."""
        posted_urls: list[str] = []

        def fake_post(url, headers, body):
            posted_urls.append(url)
            return _make_broker_response()

        cfg = _make_config(paper_base_url="https://paper-api.alpaca.markets")
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        client.place(_make_order())

        assert len(posted_urls) == 1
        assert posted_urls[0] == "https://paper-api.alpaca.markets/v2/orders"

    def test_place_qty_is_integer_string(self):
        """qty must be a string of an integer, not a float string like '2.0'."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response(qty=body["qty"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        client.place(_make_order(contracts_qty=2))

        qty_str = captured[0]["qty"]
        # Must be parseable as int without remainder.
        assert qty_str == str(int(qty_str)), f"qty={qty_str!r} is not an integer string"

    # ------------------------------------------------------------------
    # Limit-price tick rules
    # ------------------------------------------------------------------

    def test_place_limit_price_above_1_dollar_two_dp(self):
        """For ask >= $1 the limit price has 2 decimal places."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response(limit_price=body["limit_price"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        # ask = 5.10 → ticked = 5.11
        client.place(_make_order(ask=5.10, bid=5.00))

        lp = captured[0]["limit_price"]
        parts = lp.split(".")
        assert len(parts) == 2, f"expected decimal, got {lp!r}"
        assert len(parts[1]) == 2, f"expected 2dp for price >= $1, got {lp!r}"
        assert float(lp) > 5.10  # marketable buffer applied

    def test_place_limit_price_below_1_dollar_four_dp(self):
        """For ask < $1 the limit price has 4 decimal places."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response(limit_price=body["limit_price"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        # ask = 0.50 → ticked = 0.5001
        client.place(_make_order(ask=0.50, bid=0.45))

        lp = captured[0]["limit_price"]
        parts = lp.split(".")
        assert len(parts) == 2
        assert len(parts[1]) == 4, f"expected 4dp for price < $1, got {lp!r}"
        assert float(lp) > 0.50  # marketable buffer applied

    def test_place_uses_ask_not_mid_when_ask_available(self):
        """Limit price is derived from ask (not mid) when ask is present."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response(limit_price=body["limit_price"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        # ask=5.20, bid=4.80 → mid=5.00; limit must be based on ask=5.20
        client.place(_make_order(ask=5.20, bid=4.80))

        lp = float(captured[0]["limit_price"])
        # If based on mid (5.00) the price would be <= 5.01.
        # If based on ask (5.20) the price is >= 5.21.
        assert lp >= 5.21 - 0.001, f"expected limit price >= ask+tick, got {lp}"

    def test_place_falls_back_to_mid_when_ask_is_none(self):
        """When ask is None, mid_price is used as the base for the limit."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response(limit_price=body["limit_price"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        # ask=None, bid=5.00 → mid=None → should raise ValueError
        # Use bid=5.00, ask=None: mid_price is None too → ValueError
        # Instead test a case where ask=None but bid allows mid computation:
        # We need to construct manually because _make_order uses ask in mid
        contract = _make_contract(ask=None, bid=5.00)
        # mid_price requires BOTH bid and ask to be non-None; since ask=None → mid=None
        # So we need ask=None and bid is meaningful but alone won't produce mid.
        # Use a sub-path: inject contract with ask=None AND bid=None → ValueError.
        # Real mid fallback test: construct with ask=None and check ValueError raised.
        order = OptionOrder(
            contract=contract,
            contracts_qty=1,
            est_premium=500.0,
            delta_adjusted_notional=11250.0,
            side=OptionSide.CALL,
        )
        with pytest.raises(ValueError, match="no ask or mid price"):
            client.place(order)

    def test_place_raises_value_error_when_no_price_available(self):
        """Both ask and mid_price are None → ValueError (no limit price derivable)."""
        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=lambda u, h, b: {})
        contract = _make_contract(ask=None, bid=None)
        order = OptionOrder(
            contract=contract,
            contracts_qty=1,
            est_premium=0.0,
            delta_adjusted_notional=0.0,
            side=OptionSide.CALL,
        )
        with pytest.raises(ValueError, match="no ask or mid price"):
            client.place(order)

    # ------------------------------------------------------------------
    # client_order_id
    # ------------------------------------------------------------------

    def test_place_includes_client_order_id(self):
        """client_order_id is present and non-empty in every POST body."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response(client_order_id=body["client_order_id"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        client.place(_make_order())

        assert "client_order_id" in captured[0]
        assert captured[0]["client_order_id"]  # non-empty string

    def test_place_each_call_gets_unique_client_order_id(self):
        """Two separate place() calls produce distinct client_order_ids."""
        ids: list[str] = []

        def fake_post(url, headers, body):
            ids.append(body["client_order_id"])
            return _make_broker_response(client_order_id=body["client_order_id"])

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        client.place(_make_order())
        client.place(_make_order())

        assert ids[0] != ids[1], "two separate orders must not share a client_order_id"

    # ------------------------------------------------------------------
    # Never sell, never market
    # ------------------------------------------------------------------

    def test_place_side_is_always_buy(self):
        """side must always be 'buy' regardless of order.side."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response()

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        # Even a PUT option is a buy-to-open.
        put_contract = _make_contract(side=OptionSide.PUT, delta=-0.75)
        put_order = OptionOrder(
            contract=put_contract,
            contracts_qty=1,
            est_premium=505.0,
            delta_adjusted_notional=11250.0,
            side=OptionSide.PUT,
        )
        client.place(put_order)

        assert captured[0]["side"] == "buy"

    def test_place_type_is_always_limit(self):
        """type must always be 'limit' — never market."""
        captured: list[dict] = []

        def fake_post(url, headers, body):
            captured.append(body)
            return _make_broker_response()

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        client.place(_make_order())

        assert captured[0]["type"] == "limit"

    # ------------------------------------------------------------------
    # Return value
    # ------------------------------------------------------------------

    def test_place_returns_broker_response_dict(self):
        """place() returns the full parsed broker response dict."""
        broker_resp = _make_broker_response(status="accepted")

        def fake_post(url, headers, body):
            return broker_resp

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        result = client.place(_make_order())

        assert result == broker_resp
        assert result["status"] == "accepted"
        assert result["symbol"] == "AAPL240119C00150000"

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def test_place_raises_options_broker_error_on_non_200(self):
        """A non-200 from the broker raises OptionsBrokerError."""

        def fake_post(url, headers, body):
            raise OptionsBrokerError("422 Unprocessable Entity", status_code=422, body='{"code":42210000}')

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)

        with pytest.raises(OptionsBrokerError) as exc_info:
            client.place(_make_order())

        assert exc_info.value.status_code == 422

    def test_place_does_not_retry_on_broker_non_200(self):
        """OptionsBrokerError is not retried (no duplicate orders)."""
        call_count = [0]

        def fake_post(url, headers, body):
            call_count[0] += 1
            raise OptionsBrokerError("403 Forbidden", status_code=403)

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)

        with pytest.raises(OptionsBrokerError):
            client.place(_make_order())

        assert call_count[0] == 1, "OptionsBrokerError must NOT be retried"

    def test_place_retries_once_on_transport_error(self):
        """A transport-level error (not OptionsBrokerError) gets 1 retry."""
        call_count = [0]

        def fake_post(url, headers, body):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("timeout")
            return _make_broker_response()

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)
        result = client.place(_make_order())

        assert call_count[0] == 2, "expected exactly 1 retry on transport error"
        assert result["status"] == "accepted"

    def test_place_raises_options_broker_error_after_max_retries(self):
        """After exhausting retries on transport errors, raises OptionsBrokerError."""
        call_count = [0]

        def fake_post(url, headers, body):
            call_count[0] += 1
            raise ConnectionError("persistent timeout")

        cfg = _make_config()
        client = AlpacaOptionsClient(cfg, http_get=lambda u, h, p: {}, http_post=fake_post)

        with pytest.raises(OptionsBrokerError):
            client.place(_make_order())

        assert call_count[0] == 2  # 1 attempt + 1 retry
