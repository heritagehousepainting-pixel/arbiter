# J1 — Secrets & Credential Handling Audit

**Lane:** J1 (secrets & credential handling)
**Scope:** How API keys & secrets are loaded, stored, logged, and exposed. Egress/SSRF/injection are out of scope (J2).
**Auditor:** READ-ONLY. No source/test/config modified.
**Date:** 2026-06-19
**System:** `/Users/jonathanmorris/poly_bot/arbiter`

---

## Verdict

**CONDITIONAL PASS — with two P1 leak vectors that should be fixed before live trading.**

The fundamentals are sound: the repo is **not** git-tracked, `.env` is gitignored and `chmod 600`, `.env.example` carries no real values, and no code currently writes a secret value to logs / `audit.jsonl` / metrics / the web dashboard. Secrets are transmitted only as HTTP request headers, and `httpx` exception strings do not include those headers.

However, two latent/active leak vectors exist that an attacker with **read access to process logs** could exploit:

1. **P1** — `KILL_SWITCH_URL` / `ALERT_WEBHOOK_URL` are logged verbatim (`url=url`) on every fetch/failure. If either is a tokened webhook (Slack/Discord/etc. embed a secret in the URL path), the secret lands in stdout JSON logs.
2. **P1** — The frozen `Config` dataclass auto-generates a `__repr__` that includes `alpaca_api_key` / `alpaca_secret_key` in cleartext. No current call site interpolates a `Config` into a log/exception, but it is a one-line footgun away from dumping both Alpaca credentials.

Plus a **P2** test-hygiene issue: `load_config()` mutates the global `os.environ` with all `.env` keys (secrets included) via `setdefault`, and there is no autouse fixture to snapshot/restore it — verified empirically below.

---

## Findings

### P1 — Tokened alert/kill-switch URLs logged verbatim — `arbiter/safety/alerting.py:183`, `arbiter/safety/kill_switch.py:119,127,137`
**Why:** `alerting.webhook_failed` logs `url=url` (line 183); `kill_switch.http_error` / `kill_switch.fetch_failed` / `kill_switch.fetched` all log `url=url` (lines 119–124, 126–133, 137). `ALERT_WEBHOOK_URL` is the canonical case of a "secret in a URL" — Slack/Discord/PagerDuty webhook URLs carry an unguessable token in the path. Any reader of stdout/log files recovers a live alerting (and potential kill-switch override) credential. Logs are JSON to stdout (`logging_setup.py`) and are also surfaced indirectly in operational tooling.
**Recommended action:** Never log the full URL for these endpoints. Log only the host (`urlsplit(url).hostname`) or a redacted form, or drop the `url=` field entirely. Apply to all four log sites.

### P1 — `Config.__repr__` exposes Alpaca key + secret in cleartext — `arbiter/config.py:88–134`
**Why:** `@dataclass(frozen=True) class Config` includes `alpaca_api_key` and `alpaca_secret_key` as plain `str` fields. The auto-generated `__repr__`/`__str__` prints every field's value. Verified: `repr(load_config())` contains the literal secret-key field values. Any future `log.info("...", config=config)`, `f"{config}"`, `print(config)`, or an unhandled exception whose traceback locals include `config` would dump both Alpaca credentials into logs / `audit.jsonl` / the dashboard. Currently no call site does this (grep clean), so it is latent — but it is the single highest-impact footgun in the secret path.
**Recommended action:** Override `__repr__` on `Config` to redact secret fields (e.g. mask `alpaca_api_key`/`alpaca_secret_key` to `***`), or wrap secrets in a `SecretStr`-style type whose `repr` is masked. Add a unit test asserting `"<secret-value>" not in repr(config)`.

### P2 — `_load_dotenv` mutates global `os.environ`; no test-env isolation — `arbiter/config.py:163–182`, `tests/conftest.py`
**Why:** `_load_dotenv` does `os.environ.setdefault(key, value)` for every `.env` line. The first `load_config()` call in a test session permanently injects the real Alpaca key/secret (and all other `.env` vars) into the process `os.environ` for the remainder of the run. Empirically confirmed: after `load_config()`, `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `ALERT_WEBHOOK_URL`, `KILL_SWITCH_URL`, etc. are newly present in `os.environ`. The root `tests/conftest.py` has **no** autouse fixture snapshotting/restoring `os.environ`. This previously caused `ARBITER_*` leakage between tests; `tests/test_config.py` works around it locally (monkeypatches `_load_dotenv` to a no-op + `delenv`s), but every other test that calls `load_config()` silently pollutes global state and could read live secrets where a test intended none.
**Recommended action:** Add an autouse fixture in `tests/conftest.py` that snapshots `os.environ` and restores it after each test (`monkeypatch` does this automatically only for vars it sets, not for `setdefault` side-effects). Optionally make `_load_dotenv` accept an explicit mapping to populate rather than mutating the global.

### P2 — No secret-rotation / provenance story; paper secret pasted in plaintext — `arbiter/.env`, `SETUP_NEEDED.md`
**Why:** The Alpaca paper key/secret live as plaintext in `arbiter/.env` (was pasted in plaintext during setup). There is no documented rotation procedure, no secret-manager integration, and the same load path will serve **live** money keys when `LIVE_TRADING=true` (`engine.py:1304` asserts both are present). A plaintext-on-disk secret with no rotation runbook is acceptable for paper, but is a gap before any live key is introduced. `.env` is `chmod 600` (good) and gitignored (good), so blast radius today is limited to local-disk/log read access.
**Recommended action:** Document a rotation runbook (revoke in Alpaca dashboard → update `.env` → restart). Before live trading, move live keys to an OS keychain / secret manager rather than `.env`, and treat the currently-pasted paper secret as compromised-by-default (rotate it once).

### P3 — Secrets transit as headers but exception strings are header-safe (informational) — `arbiter/execution/alpaca_adapter.py:113–154`, `arbiter/data/sources/alpaca.py:98`, `arbiter/data/current_price.py:92`, `arbiter/runtime/market_calendar.py:173`
**Why:** Alpaca key/secret are sent as `APCA-API-KEY-ID` / `APCA-API-SECRET-KEY` headers. Error sites log `error=str(exc)` and `url=url`. Confirmed `httpx` `RequestError`/`HTTPStatusError` string forms do **not** include request headers, so the keys are not leaked here today. The residual risk is purely defensive: a future switch to a client/library whose exception repr includes headers, or code that logs `response.request.headers`, would leak. No action required now.
**Recommended action:** None required. Keep an eye on it if the HTTP client is swapped; never log `request.headers`.

### P3 — `MIROFISH_ENDPOINT` is a non-secret internal URL (informational) — `arbiter/adapters/mirofish/http_client.py:51,145`
**Why:** `MIROFISH_ENDPOINT` is read from env and is a self-hosted/localhost filing-data endpoint (no embedded credential). Logging/erroring on it (e.g. "MIROFISH_ENDPOINT is not set") reveals only an internal hostname, not a secret. Low concern.
**Recommended action:** None. If a future MiroFish deployment puts a token in the endpoint URL, revisit under the P1 URL-logging rule above.

---

## What an attacker with read access could recover

- **Logs (stdout/JSON):** TODAY — the `ALERT_WEBHOOK_URL` (and `KILL_SWITCH_URL`) values, including any embedded token (P1). NOT the Alpaca key/secret (header-only; exception strings are header-safe). FUTURE/one-edit-away — the full Alpaca key+secret if any code ever logs a `Config` (P1 repr).
- **`audit.jsonl` / metrics:** No secret material today — audit/metrics payloads are decision/order data, and the dashboard renders only audit payloads (HTML-escaped via `_e`). Clean.
- **Web dashboard:** No config/secret fields rendered; only mode banner, orders, leaderboard, safety, and audit tail. Clean.
- **DB:** No secrets persisted to SQLite. Clean.
- **Disk:** `arbiter/.env` (plaintext, `chmod 600`) — local filesystem read recovers everything.

---

## OPPORTUNITIES TO ADD

- **`SecretStr` wrapper type** for `alpaca_api_key` / `alpaca_secret_key` (and any future tokened URL) whose `__repr__`/`__str__` masks the value and requires `.reveal()`/`.get_secret_value()` to read — kills the P1 repr footgun structurally.
- **A startup self-test / unit test** asserting no secret value appears in `repr(config)`, in a sample log line, or in a rendered dashboard page (regression guard).
- **A `redact_url()` helper** (host-only) used at all four kill-switch/alerting log sites and any future webhook logging.
- **Autouse `os.environ` snapshot/restore fixture** in `tests/conftest.py` to end the `_load_dotenv` global-mutation leak across the suite.
- **Pre-commit / CI secret scanner** (e.g. gitleaks/trufflehog) even though the repo is untracked today — protects against an accidental `git init && add .` that would stage `.env`.
- **Rotation runbook** in `SETUP_NEEDED.md`, and move live keys off `.env` into a secret manager before `LIVE_TRADING=true`.
