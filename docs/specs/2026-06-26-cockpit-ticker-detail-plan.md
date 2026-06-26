# Cockpit — click-to-expand ticker detail: IMPLEMENTATION PLAN

**Date:** 2026-06-26  
**Branch:** cockpit-ticker-detail  
**Status:** APPROVED → ready to build  
**Depends on spec:** `docs/specs/2026-06-26-cockpit-ticker-detail-design.md`

---

## Verified constants (from config.py + current_price.py)

| Item | Exact value / attribute |
|---|---|
| Trading base config attribute | `config.alpaca_paper_base_url` |
| Data base config attribute | `config.alpaca_data_base_url` |
| Data feed (env, no Config field) | `os.getenv("ALPACA_DATA_FEED", "iex")` |
| Auth header keys | `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` |
| Executor HTTP method | `ex.http_get(url, headers)` |
| Executor trading base | `ex._base()` (returns `alpaca_paper_base_url`) |
| Executor auth headers | `ex._headers()` |
| `change_today` in raw position | fraction, e.g. `0.0099` = 0.99 % |

**Assets endpoint:** `GET {ex._base()}/v2/assets/{SYMBOL}` → JSON field `name`  
**Bars endpoint:** `GET {config.alpaca_data_base_url.rstrip("/")}/v2/stocks/{SYMBOL}/bars?timeframe=1Day&start={35d_ago_iso}&feed=iex&adjustment=all` → JSON field `bars` (list of `{t, o, h, l, c, v, n, vw}`, ascending time order)

---

## DO NOT CHANGE checklist

- Existing `OpenPosition` fields (ticker, side, qty, avg_entry, current_price, market_value, cost_basis, unrealized_pl, unrealized_pl_pct) — no rename, no removal.
- Existing positions table columns in `PositionsPanel.tsx` (Ticker, Side, Shares, Cost/sh, Current, ROI, P&L).
- `Portfolio`, `PositionsResponse`, all other DTOs.
- `/positions` 5-second poll — no extra load (ticker detail is lazy, never called by the poll).
- The constellation scene, node styles, other panels (Options, health).
- `db.py` — cockpit is read-only; `ticker.py` never opens SQLite.
- Any arbiter source file outside `cockpit/`.

---

## Step-by-step plan (TDD — tests precede implementation in every step)

---

### Step 1 — Add `day_change_pct` to `OpenPosition` DTO (contract + positions)

**Goal:** Surface Alpaca's already-available `change_today` fraction on every position row without an extra API call.

#### 1a. Tests first — `cockpit/api/test_ticker.py` (new file)

Create `cockpit/api/test_ticker.py`. Write a `TestDayChangePct` class with these tests:

```python
class TestDayChangePct:
    def test_day_change_pct_present_when_change_today_supplied(self, client):
        """When raw /v2/positions carries change_today, it appears in the response."""
        # Patch build_positions to inject a controlled raw payload
        with patch("cockpit.api.positions.build_positions") as mock_bp:
            from cockpit.api.contract import OpenPosition, Portfolio, PositionsResponse
            mock_bp.return_value = PositionsResponse(
                positions=[
                    OpenPosition(
                        ticker="MS", side="long", qty=10.0, avg_entry=100.0,
                        current_price=101.0, day_change_pct=0.0099,
                    )
                ],
                portfolio=Portfolio(n_open=1),
                as_of="2026-06-26T00:00:00Z",
                alpaca_ok=True,
            )
            r = client.get("/positions")
        assert r.status_code == 200
        positions = r.json()["positions"]
        assert positions[0]["day_change_pct"] == pytest.approx(0.0099, abs=1e-6)

    def test_day_change_pct_null_when_change_today_absent(self, client):
        """When raw /v2/positions has no change_today, day_change_pct is None."""
        with patch("cockpit.api.positions.build_positions") as mock_bp:
            from cockpit.api.contract import OpenPosition, Portfolio, PositionsResponse
            mock_bp.return_value = PositionsResponse(
                positions=[
                    OpenPosition(
                        ticker="MS", side="long", qty=10.0, avg_entry=100.0,
                    )
                ],
                portfolio=Portfolio(n_open=1),
                as_of="2026-06-26T00:00:00Z",
                alpaca_ok=True,
            )
            r = client.get("/positions")
        assert r.status_code == 200
        assert r.json()["positions"][0]["day_change_pct"] is None
```

Also write a unit-level test for `positions.py`'s raw parsing in `test_ticker.py`:

```python
class TestPositionsParser:
    def test_change_today_fraction_parsed(self):
        """positions.py _f(p.get('change_today')) returns a float fraction."""
        # Import and call directly with mocked arbiter plumbing
        with patch("arbiter.config.load_config") as mock_cfg_fn, \
             patch("arbiter.engine.build_executor") as mock_ex_fn:
            mock_ex = MagicMock()
            mock_ex.http_get.return_value = [{
                "symbol": "MS", "qty": "10.0", "side": "long",
                "avg_entry_price": "100.0", "current_price": "101.0",
                "market_value": "1010.0", "cost_basis": "1000.0",
                "unrealized_pl": "10.0", "unrealized_plpc": "0.01",
                "change_today": "0.0099",
            }]
            mock_ex._base.return_value = "https://paper-api.alpaca.markets"
            mock_ex._headers.return_value = {}
            mock_ex.get_account.return_value = MagicMock(equity=10000.0, daily_pl=5.0)
            mock_ex_fn.return_value = mock_ex
            mock_cfg_fn.return_value = MagicMock()

            from cockpit.api.positions import build_positions
            result = build_positions()

        assert len(result.positions) == 1
        assert result.positions[0].day_change_pct == pytest.approx(0.0099, abs=1e-6)

    def test_change_today_absent_gives_none(self):
        """change_today missing from raw payload → day_change_pct = None."""
        with patch("arbiter.config.load_config") as mock_cfg_fn, \
             patch("arbiter.engine.build_executor") as mock_ex_fn:
            mock_ex = MagicMock()
            mock_ex.http_get.return_value = [{
                "symbol": "MS", "qty": "10.0", "avg_entry_price": "100.0",
                "current_price": "101.0", "market_value": "1010.0",
                "cost_basis": "1000.0", "unrealized_pl": "10.0",
                "unrealized_plpc": "0.01",
                # no change_today key
            }]
            mock_ex._base.return_value = "https://paper-api.alpaca.markets"
            mock_ex._headers.return_value = {}
            mock_ex.get_account.return_value = MagicMock(equity=10000.0, daily_pl=None)
            mock_ex_fn.return_value = mock_ex
            mock_cfg_fn.return_value = MagicMock()

            from cockpit.api.positions import build_positions
            result = build_positions()

        assert result.positions[0].day_change_pct is None
```

**Verify command (write tests, then run to see them RED):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit && ../arbiter/.venv/bin/python -m pytest api/test_ticker.py -q 2>&1 | head -40
```

#### 1b. Implementation — `cockpit/api/contract.py`

In the `OpenPosition` model add one field after `unrealized_pl_pct`:

```python
day_change_pct: float | None = None   # day % as a fraction (e.g. 0.0099 = 0.99 %)
```

#### 1c. Implementation — `cockpit/web/src/contract.ts`

In the `OpenPosition` interface add one field after `unrealized_pl_pct`:

```typescript
day_change_pct: number | null;  // day % as a fraction (e.g. 0.0099 = 0.99 %)
```

Also update the `FIXTURE` in `PositionsPanel.test.tsx` to include `day_change_pct` (use `null` for existing entries, then in Step 6 add non-null values for new test cases).

#### 1d. Implementation — `cockpit/api/positions.py`

In the `OpenPosition(...)` constructor call (line 64–67), add:

```python
day_change_pct=_f(p.get("change_today")),
```

The `_f` helper already handles absent keys and non-numeric values gracefully.

**Verify (tests GREEN):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit && ../arbiter/.venv/bin/python -m pytest api/test_ticker.py::TestDayChangePct api/test_ticker.py::TestPositionsParser -q
npx tsc -b   # from cockpit/web/
```

---

### Step 2 — Add `TickerDetail` DTO

**Goal:** Define the new DTO in both Python and TypeScript before writing any implementation.

#### 2a. Tests first — `cockpit/api/test_ticker.py`

Add a schema validation test (can be a simple import check at this stage):

```python
class TestTickerDetailSchema:
    def test_ticker_detail_can_be_constructed_minimal(self):
        """TickerDetail with only required fields is valid."""
        from cockpit.api.contract import TickerDetail
        d = TickerDetail(symbol="MS", as_of="2026-06-26T00:00:00Z")
        assert d.symbol == "MS"
        assert d.name is None
        assert d.month_return_pct is None
        assert d.current_price is None

    def test_ticker_detail_can_be_constructed_full(self):
        from cockpit.api.contract import TickerDetail
        d = TickerDetail(
            symbol="MS", name="Morgan Stanley",
            month_return_pct=0.0423, current_price=101.5,
            as_of="2026-06-26T00:00:00Z",
        )
        assert d.name == "Morgan Stanley"
        assert d.month_return_pct == pytest.approx(0.0423, abs=1e-6)
```

#### 2b. Implementation — `cockpit/api/contract.py`

After the `PositionsResponse` class (and before the `# --- Options layer` block), add:

```python
class TickerDetail(BaseModel):
    """Company name + 1-month return for one held ticker (lazy, per-expand)."""
    symbol: str                              # always upper-cased
    name: str | None = None                  # from GET /v2/assets/{symbol}
    month_return_pct: float | None = None    # (latest_bar_close - oldest_bar_close) / oldest_bar_close
    current_price: float | None = None       # echoed from latest daily bar close
    as_of: str                               # UTC ISO timestamp of fetch
```

#### 2c. Implementation — `cockpit/web/src/contract.ts`

After the `PositionsResponse` interface (and before `// --- Options layer`), add:

```typescript
export interface TickerDetail {
  symbol: string;                    // always upper-cased
  name: string | null;               // from GET /v2/assets/{symbol}
  month_return_pct: number | null;   // (latest_bar_close - oldest_bar_close) / oldest_bar_close
  current_price: number | null;      // echoed from latest daily bar close
  as_of: string;                     // UTC ISO timestamp of fetch
}
```

**Verify:**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit && ../arbiter/.venv/bin/python -m pytest api/test_ticker.py::TestTickerDetailSchema -q
cd cockpit/web && npx tsc -b
```

---

### Step 3 — Create `cockpit/api/ticker.py`

**Goal:** Implement `build_ticker_detail(symbol)` that hits two read-only Alpaca endpoints and degrades gracefully to null fields on any failure.

#### 3a. Tests first — add `TestTickerDetail` to `cockpit/api/test_ticker.py`

```python
# Helper factory shared across all ticker tests
def _make_mock_ex(http_get_side_effect):
    mock_ex = MagicMock()
    mock_ex._base.return_value = "https://paper-api.alpaca.markets"
    mock_ex._headers.return_value = {"APCA-API-KEY-ID": "test", "APCA-API-SECRET-KEY": "test"}
    mock_ex.http_get.side_effect = http_get_side_effect
    return mock_ex

def _mock_cfg():
    cfg = MagicMock()
    cfg.alpaca_data_base_url = "https://data.alpaca.markets"
    return cfg

# Realistic bars fixture (35-day window, oldest first, ascending)
_BARS_FIXTURE = {
    "bars": [
        {"t": "2026-05-22T04:00:00Z", "o": 98.0, "h": 103.0, "l": 97.0, "c": 100.0},
        {"t": "2026-06-25T04:00:00Z", "o": 110.0, "h": 115.0, "l": 109.0, "c": 112.0},
    ],
    "symbol": "MS",
    "next_page_token": None,
}
_ASSET_FIXTURE = {"name": "Morgan Stanley", "symbol": "MS", "status": "active"}


class TestTickerDetail:
    # ---- happy path ---------------------------------------------------------

    def test_happy_path_name_and_month_return(self):
        """Mocked assets + bars → correct name and month_return_pct."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return _ASSET_FIXTURE
            if "/bars" in url:
                return _BARS_FIXTURE
            return {}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("ms")  # lowercase → must upper-case

        assert detail.symbol == "MS"
        assert detail.name == "Morgan Stanley"
        expected_month = (112.0 - 100.0) / 100.0  # (latest_close - oldest_close) / oldest_close
        assert detail.month_return_pct == pytest.approx(expected_month, abs=1e-6)
        assert detail.current_price == pytest.approx(112.0, abs=1e-6)
        assert detail.as_of  # non-empty

    def test_symbol_is_upper_cased(self):
        """Any casing of input symbol → TickerDetail.symbol is upper."""
        def http_get(url, headers):
            return {} if "/assets/" in url else {"bars": []}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("aapl")

        assert detail.symbol == "AAPL"

    def test_url_contains_correct_symbol(self):
        """The HTTP call to assets endpoint uses the upper-cased symbol."""
        called_urls = []

        def http_get(url, headers):
            called_urls.append(url)
            if "/v2/assets/" in url:
                return {"name": "Apple Inc.", "symbol": "AAPL", "status": "active"}
            return {"bars": []}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            build_ticker_detail("aapl")

        assets_calls = [u for u in called_urls if "/v2/assets/" in u]
        assert len(assets_calls) == 1
        assert assets_calls[0].endswith("/v2/assets/AAPL")

    def test_bars_url_has_required_params(self):
        """Bars URL contains timeframe, start, feed, adjustment params."""
        called_urls = []

        def http_get(url, headers):
            called_urls.append(url)
            if "/v2/assets/" in url:
                return {"name": "Test Corp", "symbol": "TST"}
            return {"bars": []}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            build_ticker_detail("TST")

        bars_calls = [u for u in called_urls if "/bars" in u]
        assert len(bars_calls) == 1
        u = bars_calls[0]
        assert "timeframe=1Day" in u
        assert "start=" in u
        assert "adjustment=all" in u
        # feed must be present (iex by default or from env)
        assert "feed=" in u

    # ---- degrade paths ------------------------------------------------------

    def test_alpaca_down_returns_null_fields_http200(self):
        """Any exception from http_get → null name + null month_return_pct, HTTP 200."""
        def http_get(url, headers):
            raise RuntimeError("connection refused")

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.symbol == "MS"
        assert detail.name is None
        assert detail.month_return_pct is None
        assert detail.current_price is None

    def test_load_config_raises_returns_null_detail(self):
        """Exception in load_config (no .env) → null-field TickerDetail, no crash."""
        with patch("arbiter.config.load_config", side_effect=Exception("no config")):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.symbol == "MS"
        assert detail.name is None
        assert detail.month_return_pct is None

    def test_missing_bars_gives_null_month_return(self):
        """bars key present but list is empty → month_return_pct = None."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Morgan Stanley", "symbol": "MS"}
            return {"bars": [], "next_page_token": None}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.name == "Morgan Stanley"  # name still populated
        assert detail.month_return_pct is None
        assert detail.current_price is None

    def test_single_bar_gives_null_month_return(self):
        """Only 1 bar in window → cannot compute return, month_return_pct = None."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Morgan Stanley", "symbol": "MS"}
            return {"bars": [{"t": "2026-06-25T04:00:00Z", "c": 112.0}]}

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.month_return_pct is None

    def test_bars_missing_key_returns_none_gracefully(self):
        """If bars response lacks 'bars' key entirely, month_return_pct = None."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"name": "Corp", "symbol": "X"}
            return {}  # no 'bars' key

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("X")

        assert detail.month_return_pct is None

    def test_assets_returns_no_name_key(self):
        """Assets response without 'name' → name = None, no crash."""
        def http_get(url, headers):
            if "/v2/assets/" in url:
                return {"symbol": "MS", "status": "active"}  # no 'name'
            return _BARS_FIXTURE

        with patch("arbiter.config.load_config", return_value=_mock_cfg()), \
             patch("arbiter.engine.build_executor", return_value=_make_mock_ex(http_get)):
            from cockpit.api.ticker import build_ticker_detail
            detail = build_ticker_detail("MS")

        assert detail.name is None
        assert detail.month_return_pct is not None  # bars still computed
```

#### 3b. Implementation — `cockpit/api/ticker.py` (new file)

```python
"""Lazy ticker detail — company name + 1-month return for one symbol.

Called ONLY on accordion expand; never polled.  Degrades to null fields
on any Alpaca failure (HTTP 200 always returned, never 404 / 500).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

from .contract import TickerDetail
from .db import DEFAULT_DB_PATH

_ARBITER_PKG_ROOT = DEFAULT_DB_PATH.parents[1]  # <repo>/arbiter


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _f(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def build_ticker_detail(symbol: str) -> TickerDetail:
    """Fetch company name + 1-month bars return for *symbol*.

    Returns a TickerDetail with null fields on any error — never raises.
    Symbol is always upper-cased in the returned DTO.
    """
    sym = symbol.strip().upper()
    if not sym:
        return TickerDetail(symbol=sym, as_of=_now())

    try:
        if str(_ARBITER_PKG_ROOT) not in sys.path:
            sys.path.insert(0, str(_ARBITER_PKG_ROOT))
        from arbiter.config import load_config  # noqa: PLC0415
        from arbiter.engine import build_executor  # noqa: PLC0415

        cfg = load_config()
        ex = build_executor(cfg)
        data_base = cfg.alpaca_data_base_url.rstrip("/")
    except Exception:
        return TickerDetail(symbol=sym, as_of=_now())

    # --- Company name via trading API ----------------------------------------
    name: str | None = None
    try:
        asset = ex.http_get(  # type: ignore[attr-defined]
            f"{ex._base()}/v2/assets/{sym}",  # type: ignore[attr-defined]
            ex._headers(),  # type: ignore[attr-defined]
        )
        if isinstance(asset, dict):
            name = asset.get("name") or None
    except Exception:
        pass  # name stays None

    # --- 1-month bars via data API -------------------------------------------
    month_return_pct: float | None = None
    current_price: float | None = None
    try:
        start_iso = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d")
        feed = os.getenv("ALPACA_DATA_FEED", "iex")
        bars_url = (
            f"{data_base}/v2/stocks/{sym}/bars"
            f"?timeframe=1Day&start={start_iso}&feed={feed}&adjustment=all"
        )
        bars_resp = ex.http_get(bars_url, ex._headers())  # type: ignore[attr-defined]
        bars = (bars_resp or {}).get("bars") or []

        if len(bars) >= 2:
            ref_close = _f((bars[0] or {}).get("c"))
            latest_close = _f((bars[-1] or {}).get("c"))
            if ref_close is not None and ref_close > 0 and latest_close is not None:
                month_return_pct = (latest_close - ref_close) / ref_close
                current_price = latest_close
    except Exception:
        pass  # month fields stay None

    return TickerDetail(
        symbol=sym,
        name=name,
        month_return_pct=month_return_pct,
        current_price=current_price,
        as_of=_now(),
    )
```

**Verify (tests GREEN):**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit && ../arbiter/.venv/bin/python -m pytest api/test_ticker.py::TestTickerDetail -q
```

---

### Step 4 — Register `GET /ticker/{symbol}` route in `main.py`

**Goal:** Wire the route; verify it returns HTTP 200 with null fields when Alpaca is offline.

#### 4a. Tests first — add `TestTickerRoute` to `cockpit/api/test_ticker.py`

```python
class TestTickerRoute:
    def test_ticker_route_returns_200_offline(self, client):
        """GET /ticker/{symbol} with Alpaca offline → 200 with null fields."""
        with patch("cockpit.api.ticker.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(symbol="MS", as_of="2026-06-26T00:00:00Z")
            r = client.get("/ticker/MS")

        assert r.status_code == 200
        data = r.json()
        assert data["symbol"] == "MS"
        assert data["name"] is None
        assert data["month_return_pct"] is None
        assert data["current_price"] is None
        assert data["as_of"]

    def test_ticker_route_returns_200_live(self, client):
        """GET /ticker/{symbol} with mocked live data → 200 with populated fields."""
        with patch("cockpit.api.ticker.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(
                symbol="MS", name="Morgan Stanley",
                month_return_pct=0.042, current_price=112.0,
                as_of="2026-06-26T00:00:00Z",
            )
            r = client.get("/ticker/ms")  # lowercase input

        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "Morgan Stanley"
        assert data["month_return_pct"] == pytest.approx(0.042, abs=1e-6)

    def test_ticker_route_upper_cases_symbol(self, client):
        """Route passes upper-cased symbol to build_ticker_detail."""
        with patch("cockpit.api.ticker.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(symbol="AAPL", as_of="2026-06-26T00:00:00Z")
            client.get("/ticker/aapl")

        mock_build.assert_called_once_with("AAPL")

    def test_ticker_route_schema(self, client):
        """Response contains all required TickerDetail fields."""
        with patch("cockpit.api.ticker.build_ticker_detail") as mock_build:
            from cockpit.api.contract import TickerDetail
            mock_build.return_value = TickerDetail(symbol="X", as_of="2026-06-26T00:00:00Z")
            r = client.get("/ticker/X")

        data = r.json()
        for field in ("symbol", "name", "month_return_pct", "current_price", "as_of"):
            assert field in data, f"Missing field: {field}"
```

#### 4b. Implementation — `cockpit/api/main.py`

Add import and route. In the imports block, add:

```python
from .contract import Graph, Health, IVSeries, NodeDetail, OptionsState, PositionsResponse, State, TickerDetail
from .ticker import build_ticker_detail
```

Add the route after the `/positions` route:

```python
@app.get("/ticker/{symbol}", response_model=TickerDetail)
def ticker_detail(symbol: str) -> TickerDetail:
    """Company name + 1-month return for one ticker (lazy, per-expand, read-only)."""
    return build_ticker_detail(symbol.strip().upper())
```

**Verify:**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit && ../arbiter/.venv/bin/python -m pytest api/test_ticker.py -q
```

---

### Step 5 — Add `fetchTickerDetail` to `cockpit/web/src/api.ts`

**Goal:** Add the client fetch helper; verify tsc is clean before touching React.

#### 5a. Tests (TypeScript compile only at this stage)

Add import to `cockpit/web/src/api.ts` and verify `npx tsc -b` passes.

#### 5b. Implementation — `cockpit/web/src/api.ts`

Add one import to the contract import block:

```typescript
import type {
  CockpitEvent,
  Graph,
  IVSeries,
  NodeDetail,
  OptionsState,
  PositionsResponse,
  State,
  TickerDetail,
} from "./contract";
```

Add one export after `fetchIvSeries`:

```typescript
export const fetchTickerDetail = (symbol: string) =>
  get<TickerDetail>(`/ticker/${encodeURIComponent(symbol)}`);
```

**Verify:**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b
```

---

### Step 6 — Update `PositionsPanel.tsx` with inline accordion expand

**Goal:** Make each ticker cell a focusable button; one open at a time; detail fetched once and cached per session; expanded row renders name + Today(price+day%) + 1-Month.

#### 6a. Tests first — update `cockpit/web/src/ui/__tests__/PositionsPanel.test.tsx`

Replace the existing test file content. New fixture adds `day_change_pct` and a `fetchTickerDetail` mock:

```typescript
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { createRoot, type Root } from "react-dom/client";
import type { PositionsResponse, TickerDetail } from "../../contract";

// Fixtures
const FIXTURE: PositionsResponse = {
  positions: [
    {
      ticker: "AMZN", side: "long", qty: 1, avg_entry: 239.9, current_price: 234.16,
      market_value: 234.16, cost_basis: 239.9, unrealized_pl: -5.74,
      unrealized_pl_pct: -0.02393, day_change_pct: -0.0031,
    },
    {
      ticker: "UBER", side: "short", qty: 1, avg_entry: 72.19, current_price: 71.82,
      market_value: -71.82, cost_basis: 72.19, unrealized_pl: 0.37,
      unrealized_pl_pct: 0.00513, day_change_pct: 0.0099,
    },
  ],
  portfolio: {
    equity: 9994.64, cash: null, daily_pl: -5.37, n_open: 2, n_long: 1, n_short: 1,
    gross_exposure: 305.98, net_exposure: 162.34, total_cost_basis: 312.09,
    total_unrealized_pl: -5.37, total_unrealized_pl_pct: -0.0172,
  },
  as_of: "2026-06-22T00:00:00Z",
  alpaca_ok: true,
};

const AMZN_DETAIL: TickerDetail = {
  symbol: "AMZN",
  name: "Amazon.com Inc.",
  month_return_pct: 0.0423,
  current_price: 234.16,
  as_of: "2026-06-26T00:00:00Z",
};

const UBER_DETAIL: TickerDetail = {
  symbol: "UBER",
  name: "Uber Technologies Inc.",
  month_return_pct: -0.0152,
  current_price: 71.82,
  as_of: "2026-06-26T00:00:00Z",
};

// Mock both fetchPositions and fetchTickerDetail
let mockFetchTicker: ReturnType<typeof vi.fn>;

vi.mock("../../api", () => {
  mockFetchTicker = vi.fn();
  return {
    fetchPositions: vi.fn(() => Promise.resolve(FIXTURE)),
    fetchTickerDetail: mockFetchTicker,
  };
});

import { PositionsPanel } from "../PositionsPanel";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  // Default: fetchTickerDetail resolves with AMZN_DETAIL or UBER_DETAIL
  mockFetchTicker.mockImplementation((sym: string) =>
    sym === "AMZN"
      ? Promise.resolve(AMZN_DETAIL)
      : Promise.resolve(UBER_DETAIL),
  );
});

afterEach(() => {
  React.act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

async function render(el: React.ReactElement) {
  await React.act(async () => { root.render(el); });
  await React.act(async () => { await Promise.resolve(); });
}

describe("PositionsPanel", () => {
  // ---- existing behavior preserved ----------------------------------------

  it("renders existing columns: cost/share, ROI and P&L", async () => {
    await render(<PositionsPanel />);
    const txt = container.textContent ?? "";
    expect(txt).toContain("AMZN");
    expect(txt).toContain("UBER");
    expect(txt).toContain("239.90");   // cost/share
    expect(txt).toContain("-2.39%");   // AMZN ROI
    expect(txt).toContain("+0.51%");   // UBER ROI (short in profit)
  });

  it("renders portfolio summary stats", async () => {
    await render(<PositionsPanel />);
    const txt = container.textContent ?? "";
    expect(txt).toContain("1L / 1S");
    expect(txt).toContain("9994.64");
  });

  // ---- accordion toggle ---------------------------------------------------

  it("ticker cell renders as a button with aria-expanded=false initially", async () => {
    await render(<PositionsPanel />);
    const buttons = container.querySelectorAll("button[aria-expanded]");
    // At least the two ticker buttons
    const tickerButtons = Array.from(buttons).filter(
      (b) => b.textContent?.includes("AMZN") || b.textContent?.includes("UBER"),
    );
    expect(tickerButtons.length).toBeGreaterThanOrEqual(2);
    tickerButtons.forEach((b) => {
      expect(b.getAttribute("aria-expanded")).toBe("false");
    });
  });

  it("clicking ticker button sets aria-expanded=true and shows detail", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;
    expect(amznBtn).toBeTruthy();

    await React.act(async () => {
      amznBtn.click();
      await Promise.resolve();
    });

    expect(amznBtn.getAttribute("aria-expanded")).toBe("true");
  });

  it("expanded row shows company name from detail", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => {
      amznBtn.click();
      await Promise.resolve();  // flush fetchTickerDetail
    });
    // flush the detail promise
    await React.act(async () => { await Promise.resolve(); });

    expect(container.textContent).toContain("Amazon.com Inc.");
  });

  it("expanded row shows month return pct formatted as percent", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => {
      amznBtn.click();
      await Promise.resolve();
    });
    await React.act(async () => { await Promise.resolve(); });

    // +4.23% for month_return_pct = 0.0423
    expect(container.textContent).toMatch(/\+4\.23%/);
  });

  it("clicking same ticker again collapses the row", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });
    expect(amznBtn.getAttribute("aria-expanded")).toBe("true");

    await React.act(async () => { amznBtn.click(); });
    expect(amznBtn.getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).not.toContain("Amazon.com Inc.");
  });

  it("only one ticker can be open at a time", async () => {
    await render(<PositionsPanel />);
    const buttons = Array.from(container.querySelectorAll("button[aria-expanded]")).filter(
      (b) => b.textContent?.includes("AMZN") || b.textContent?.includes("UBER"),
    );
    const [amznBtn, uberBtn] = buttons as HTMLButtonElement[];

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });
    expect(amznBtn.getAttribute("aria-expanded")).toBe("true");

    await React.act(async () => { uberBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });

    expect(amznBtn.getAttribute("aria-expanded")).toBe("false");
    expect(uberBtn.getAttribute("aria-expanded")).toBe("true");
    // AMZN detail hidden, UBER detail shown
    expect(container.textContent).not.toContain("Amazon.com Inc.");
    expect(container.textContent).toContain("Uber Technologies Inc.");
  });

  // ---- loading state ------------------------------------------------------

  it("shows loading while fetchTickerDetail is in flight", async () => {
    let resolveDetail!: (v: TickerDetail) => void;
    const pending = new Promise<TickerDetail>((res) => { resolveDetail = res; });
    mockFetchTicker.mockReturnValueOnce(pending);

    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); });
    // Detail not resolved yet → loading state
    expect(container.textContent).toContain("loading");

    await React.act(async () => { resolveDetail(AMZN_DETAIL); await Promise.resolve(); });
    expect(container.textContent).not.toContain("loading");
    expect(container.textContent).toContain("Amazon.com Inc.");
  });

  // ---- null / "—" state ---------------------------------------------------

  it("null name renders as em-dash", async () => {
    mockFetchTicker.mockResolvedValueOnce({
      symbol: "AMZN", name: null, month_return_pct: null,
      current_price: null, as_of: "2026-06-26T00:00:00Z",
    } satisfies TickerDetail);

    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });

    // The panel should render "—" for null name
    // (exact em-dash character or the usd() / pct() fallback)
    expect(container.textContent).toMatch(/—/);
  });

  // ---- session cache ------------------------------------------------------

  it("fetchTickerDetail called only once per symbol across multiple expands", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    // expand → collapse → expand
    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });
    await React.act(async () => { amznBtn.click(); }); // collapse
    await React.act(async () => { amznBtn.click(); await Promise.resolve(); }); // re-expand
    await React.act(async () => { await Promise.resolve(); });

    const amznCalls = (mockFetchTicker as ReturnType<typeof vi.fn>).mock.calls.filter(
      ([sym]: [string]) => sym === "AMZN",
    );
    expect(amznCalls.length).toBe(1);  // cached after first fetch
  });
});
```

#### 6b. Implementation — `cockpit/web/src/ui/PositionsPanel.tsx`

Changes to make (described as diffs, not shown in full):

**Imports:** Add `useRef`, `fetchTickerDetail`, `TickerDetail`.

```typescript
import { useEffect, useRef, useState } from "react";
import { fetchPositions, fetchTickerDetail } from "../api";
import type { OpenPosition, PositionsResponse, TickerDetail } from "../contract";
```

**`Row` component signature:** Change to accept `openTicker`, `onToggle`, `detail`, `loading` props:

```typescript
function Row({
  p,
  isOpen,
  onToggle,
  detail,
  loadingDetail,
}: {
  p: OpenPosition;
  isOpen: boolean;
  onToggle: (ticker: string) => void;
  detail: TickerDetail | null;
  loadingDetail: boolean;
}) { ... }
```

**Row ticker cell:** Replace `<td>{p.ticker}</td>` with a focusable button:

```tsx
<td style={{ padding: "4px 10px 4px 0", fontWeight: 700 }}>
  <button
    aria-expanded={isOpen}
    onClick={() => onToggle(p.ticker)}
    style={{
      background: "none", border: 0, cursor: "pointer",
      color: C.text, fontWeight: 700, fontFamily: C.mono,
      fontSize: 11.5, padding: 0,
    }}
  >
    {isOpen ? "▾ " : "▸ "}{p.ticker}
  </button>
</td>
```

**Expanded sub-row** (immediately after the main `<tr>`, inside `<tbody>`):

```tsx
{isOpen && (
  <tr>
    <td colSpan={7} style={{ padding: "4px 10px 8px 18px", background: "rgba(28,34,51,0.7)" }}>
      {loadingDetail ? (
        <span style={{ color: C.muted, fontStyle: "italic" }}>loading…</span>
      ) : (
        <span style={{ display: "flex", gap: 20, flexWrap: "wrap", fontSize: 11 }}>
          <span style={{ color: C.muted }}>
            {detail?.name ?? "—"}
          </span>
          <span>
            <span style={{ color: C.muted, fontSize: 9, letterSpacing: 1, textTransform: "uppercase" }}>Today </span>
            <span style={{ color: plColor(p.day_change_pct), fontWeight: 700 }}>
              {usd(p.current_price)} {pct(p.day_change_pct)}
            </span>
          </span>
          <span>
            <span style={{ color: C.muted, fontSize: 9, letterSpacing: 1, textTransform: "uppercase" }}>1-Month </span>
            <span style={{ color: plColor(detail?.month_return_pct ?? null), fontWeight: 700 }}>
              {pct(detail?.month_return_pct ?? null)}
            </span>
          </span>
        </span>
      )}
    </td>
  </tr>
)}
```

**`PositionsPanel` component state:**

```typescript
const [openTicker, setOpenTicker] = useState<string | null>(null);
// Cache: symbol → TickerDetail (company name is static; fine intraday)
const detailCache = useRef<Map<string, TickerDetail>>(new Map());
const [detailMap, setDetailMap] = useState<Map<string, TickerDetail>>(new Map());
const [loadingTicker, setLoadingTicker] = useState<string | null>(null);
```

**Toggle handler:**

```typescript
const handleToggle = (ticker: string) => {
  setOpenTicker((prev) => {
    if (prev === ticker) return null;  // collapse
    // Fetch detail if not yet cached
    if (!detailCache.current.has(ticker)) {
      setLoadingTicker(ticker);
      fetchTickerDetail(ticker)
        .then((d) => {
          detailCache.current.set(ticker, d);
          setDetailMap(new Map(detailCache.current));
          setLoadingTicker(null);
        })
        .catch(() => setLoadingTicker(null));
    }
    return ticker;
  });
};
```

**Row invocation in table body:**

```tsx
{(data?.positions ?? []).map((p) => (
  <Row
    key={p.ticker}
    p={p}
    isOpen={openTicker === p.ticker}
    onToggle={handleToggle}
    detail={detailMap.get(p.ticker) ?? null}
    loadingDetail={loadingTicker === p.ticker}
  />
))}
```

**Verify:**
```bash
cd /Users/jonathanmorris/poly_bot/cockpit/web && npx tsc -b && npx vitest run
```

---

### Step 7 — Full verification pass

Run all backend tests, TypeScript compile, and all web tests.

```bash
# Backend
cd /Users/jonathanmorris/poly_bot/cockpit
../arbiter/.venv/bin/python -m pytest api/test_*.py -q

# TypeScript compile
cd /Users/jonathanmorris/poly_bot/cockpit/web
npx tsc -b

# Web tests
cd /Users/jonathanmorris/poly_bot/cockpit/web
npx vitest run
```

All three must be green before considering this complete.

---

## Risks and edge cases

| Risk | Mitigation |
|---|---|
| **Symbol casing** | `symbol.strip().upper()` at entry to both `build_ticker_detail` and the route handler; the assets + bars URLs always use the upper-cased symbol. |
| **Weekends/holidays** | 35-day calendar window guarantees ≥ 21 trading days even with holidays, long weekends, and US market closures. Never use exactly 30 days. |
| **`change_today` absent** | Alpaca may omit this field for paper accounts or certain position types. The `_f(p.get("change_today"))` pattern already returns `None` gracefully; `day_change_pct` defaults to `None`. |
| **Paper account assets endpoint** | The trading (paper) API does serve `/v2/assets/{symbol}` read-only. If it returns 401/403 (paper account restriction), the try/except in `build_ticker_detail` catches it and returns `name=None`. |
| **Fractions vs percents** | Alpaca `change_today` is a FRACTION (0.0099 = 0.99%). Frontend `pct()` helper multiplies by 100 before formatting. `month_return_pct` is also stored as a fraction and formatted by the same helper. |
| **Only 1 bar returned** | Need `len(bars) >= 2` to compute a return. Single-bar → `month_return_pct = None`. |
| **Zero ref close** | Guard: `if ref_close is not None and ref_close > 0`. |
| **Cache invalidation** | Company name is effectively static per session. Monthly return is fine stale for an intraday session (changes at most once per market day). Simple `useRef<Map>` session cache is correct; no TTL needed. |
| **Session cache grows unboundedly** | Users are unlikely to hold > 20 positions simultaneously. The cache is bounded by the position count, not time. Acceptable. |
| **Bars pagination** | Alpaca returns up to 1000 bars per page by default; a 35-day window returns ≤ 35 bars. `next_page_token` will be null; no pagination needed. |
| **tsc strict check on `detail?.month_return_pct`** | TypeScript will infer `number | null | undefined` when accessing optional chained. The `?? null` fallback ensures `pct()` always receives `number | null`. |

---

## Assumptions and decisions

1. **`build_ticker_detail` uses bars[-1].c as current_price**, not a separate latest-trade call. This matches the design's "or the latest bar close" fallback and avoids a third API call.
2. **Data feed defaults to `iex`** via `os.getenv("ALPACA_DATA_FEED", "iex")`. This mirrors `current_price.py` exactly. No new Config field is needed.
3. **Session cache via `useRef<Map>`** (not `localStorage`). Survives re-renders; cleared on page reload. Company name is static; month return accuracy within a session is adequate.
4. **`build_ticker_detail` never throws** — always returns a `TickerDetail` (possibly with null fields). The route never returns 404 or 500 for a symbol; `name=None` is the graceful degradation.
5. **`test_ticker.py` is the dedicated test module** for all ticker-related backend tests (including day_change_pct tests), keeping the growth of `test_api.py` bounded.
6. **PositionsPanel fixture update** adds `day_change_pct` to both existing fixture positions (AMZN: -0.0031, UBER: +0.0099) so TypeScript doesn't complain about the new required field.
7. **Chevron in ticker button text** (`▾`/`▸`) is the visual affordance; the `aria-expanded` attribute is the accessible affordance. Both are updated together.
