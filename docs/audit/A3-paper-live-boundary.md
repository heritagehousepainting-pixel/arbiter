# A3 — Paper → Live-Money Boundary Audit

Lane A3 (READ-ONLY). Scope: everything that keeps the system from accidentally trading REAL money.
Date: 2026-06-19
Auditor: A3 boundary lane

---

## VERDICT

**PASS — the core claim "structurally paper-only — no live base URL exists" is CONFIRMED.**
There is no live-money Alpaca trading endpoint anywhere in the package, no `live_base_url`
field on `Config`, and the only trading host the adapter can reach is
`alpaca_paper_base_url` (default `https://paper-api.alpaca.markets`). `live_trading` is
no longer consulted for broker selection (`build_executor` keys solely on
`executor_backend`), and `live_trading` defaults/stays `false`. The `executor_backend=alpaca_paper`
+ `live_trading=false` combination has no dangerous side-effect: it selects the paper adapter,
skips the SimExecutor-only seed/snapshot, leaves the kill-switch gate skipped (empty URL),
sets `paper_only=True` on every report, and fails closed on a broker equity-read failure.

The one real gap is **P1-1**: the `ALPACA_PAPER_BASE_URL` env var is overridable with **no
validation**, so the "paper-only" guarantee depends on an unvalidated string. That is the only
runtime door to a non-paper Alpaca host, and it is wide open by env. Everything else is sound.

---

## FINDINGS

### P1-1 — `ALPACA_PAPER_BASE_URL` is unvalidated; the env override is a hole in the paper-only floor — arbiter/config.py:242-245 — why — `build_executor`/`AlpacaAdapter._base()` send EVERY order to `config.alpaca_paper_base_url`. That value is overridable via the `ALPACA_PAPER_BASE_URL` env var (and the `[alpaca].paper_base_url` TOML key) with NO host/scheme validation. Setting `ALPACA_PAPER_BASE_URL=https://api.alpaca.markets` in `.env` would route all "paper" orders to the LIVE trading endpoint while every other guard (`paper_only=True`, the "SIM/PAPER" banner, the audit truth) keeps insisting they are paper. The repo-grep guarantee (no live string in source) does not cover a runtime env value. This is the single most likely way real money gets traded by accident or by an unreviewed `.env` edit. — recommended action: validate the resolved `alpaca_paper_base_url` in `load_config` — require it to be exactly `https://paper-api.alpaca.markets` (or at minimum host-match `paper-api.alpaca.markets`), raising `ConfigError` otherwise (mirror the `_VALID_EXECUTOR_BACKENDS` fail-closed pattern at config.py:223). Optionally re-assert in `AlpacaAdapter._base()` so the invariant is enforced at the seam that actually places orders.

### P2-2 — `live_trading=true` still passes `build_engine` with keys present, yet no executor honors it — arbiter/engine.py:1302-1305 — why — `build_engine` asserts only that keys exist when `live_trading=true`; it does NOT assert `executor_backend`. So a future/accidental `LIVE_TRADING=true` boots a fully-running engine that (a) flips the kill-switch gate to always-consult/fail-closed (engine.py:619), (b) flips the web/CLI banner to "LIVE" (cli.py:48, server.py:296/375/424), and (c) still selects whichever executor `executor_backend` names — i.e. `live_trading=true` + `executor_backend=sim` (or unset) runs the SIM executor while the UI screams "LIVE", and `live_trading=true` + `executor_backend=alpaca_paper` runs the PAPER adapter while the UI screams "LIVE". The flag is now semantically orphaned: it changes safety/UX behavior but selects nothing. Not a money-loss path today (no executor reaches a live host), but it is a confusing, mislabeling foot-gun that erodes the "is this live?" signal operators rely on. — recommended action: until a real-money path exists, make `build_engine` reject `live_trading=true` outright (`assert not config.live_trading, "real-money path not implemented; keep LIVE_TRADING=false"`), or assert the pairing (`live_trading` implies a not-yet-existent `executor_backend="alpaca_live"`). Fail-closed on the orphaned flag rather than letting it half-configure the system.

### P3-3 — Stale module docstrings claim LIVE_TRADING-based selection — arbiter/execution/alpaca_adapter.py:3-6; arbiter/engine.py:9-11; arbiter/execution/__init__.py:7 — why — these docstrings still say the adapter is "Selected ONLY when LIVE_TRADING=true" and the engine asserts `live_trading` False / "SimExecutor is always used unless LIVE_TRADING=true". The actual selection now keys on `executor_backend` (alpaca_adapter.py:390-396) and `live_trading` is explicitly not consulted (alpaca_adapter.py:374). A future reader auditing the boundary will be misled about which flag controls the broker — a documentation/safety-comprehension risk, not a code defect. — recommended action: update the three docstrings to describe `executor_backend`-based selection and that `live_trading` is reserved/unused.

### P3-4 — `daily_pl = equity - last_equity` can mislead in status, but is not a money path — arbiter/execution/alpaca_adapter.py:355 — why — out of A3 scope strictly, noted for completeness: `realized_pl` is hardcoded `0.0` and `daily_pl` is derived; neither affects the live/paper boundary. No action required for this lane (tracked in spec §9.6).

---

## VERIFIED-SOUND (no finding)

- **No live trading host anywhere.** Repo-wide grep for `api.alpaca.markets` excluding `paper-api`/`data.` returns NONE in source; the only `alpaca.markets` trading host is `paper-api.alpaca.markets` (config.py:244, arbiter.toml:28). `data.alpaca.markets` is market DATA only (egress.py:77), not trading.
- **`build_executor` selection (alpaca_adapter.py:390-396)** keys on `executor_backend == "alpaca_paper"` AND both keys; otherwise fail-closed `SimExecutor`. `live_trading` is correctly NOT consulted.
- **`executor_backend` is validated fail-closed** against `{"sim","alpaca_paper"}` (config.py:44, 223-227) — a typo raises `ConfigError`.
- **`AlpacaAdapter._base()` (alpaca_adapter.py:120-121)** returns ONLY `alpaca_paper_base_url`; there is no live branch. All five `ExecutionReport`/`AccountSnapshot` returns set `paper_only=True` unconditionally (lines 191, 209, 234, 265, 298, 336, 357).
- **Kill-switch gate (engine.py:619)** keys on `live_trading or kill_switch_url`, NOT on `executor_backend` — so `executor_backend=alpaca_paper` + `live_trading=false` + empty `KILL_SWITCH_URL` correctly SKIPS the gate (no halt-every-cycle), as designed. The build agent did not poison the predicate with `executor_backend`.
- **`build_engine` seed/snapshot gating (engine.py:1336, and the end-of-cycle snapshot)** keys on `isinstance(executor, SimExecutor)`, so the adapter path correctly skips the SimExecutor-only durable seed/snapshot. The old `assert isinstance(... SimExecutor)` was correctly replaced with a defensive `(SimExecutor, AlpacaAdapter)` check (engine.py:1330).
- **A2 fail-closed (engine.py:884-892)** returns a zero-order cycle when adapter equity is `None`/`<=0`, BEFORE the sizing path at engine.py:1038 — so the `100_000.0` phantom equity at 1038 is unreachable in adapter mode and remains a sim-only convenience.
- **Fill confirmation (engine.py:1114-1120)** advances an idea to MONITORED ONLY on `sub_result.filled`; `pending`/duplicate/zero-share do not advance. Sim mode is a no-op (synchronous fill).
- **Idempotency hardening:** `client_order_id = intent.order_id` is set on every order (alpaca_adapter.py:172), making the single retry idempotent at the broker.
- **Structural guard test exists and PASSES:** `tests/execution/test_alpaca_paper_mode.py::test_no_live_base_url_in_package` greps the package for a live host and asserts none (ran green, 1 passed).
- **Current `.env`:** `LIVE_TRADING=false`, `EXECUTOR_BACKEND=sim`, `ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets` — currently safe.

---

## OPPORTUNITIES TO ADD

1. **(closes P1-1) Hard host assertion on the resolved paper base URL** in `load_config` AND in `AlpacaAdapter._base()`: `assert "paper-api.alpaca.markets" in url` (or exact-match), `ConfigError`/`AssertionError` otherwise. This converts the structural guarantee from "no live STRING in source" to "no live HOST reachable at runtime," covering the env-override hole.
2. **Explicit safety test for the env-override hole:** a test that sets `ALPACA_PAPER_BASE_URL=https://api.alpaca.markets` and asserts `load_config()` (or `build_executor`/`_base()`) raises rather than silently routing to live. The current `test_no_live_base_url_in_package` only checks source strings, not config values — it would NOT catch this.
3. **(closes P2-2) Assert `not live_trading` in `build_engine`** until a real-money executor exists, so the orphaned flag can't half-configure the system (always-consult kill switch + "LIVE" banner) with no live executor behind it.
4. **Combination test** for `executor_backend=alpaca_paper` + `live_trading=false`: assert AlpacaAdapter selected, kill-switch gate skipped (empty URL), seed/snapshot NOT called, every report `paper_only=True`, banner = "SIM/PAPER". Most of these are individually covered in `test_alpaca_paper_mode.py`; a single combination assertion would lock the whole boundary.
5. **A `paper_only=False` should be unreachable** — add an invariant test asserting no `ExecutionReport`/`AccountSnapshot` produced by `AlpacaAdapter` can carry `paper_only=False`, so a future edit can't silently flip audit truth.
