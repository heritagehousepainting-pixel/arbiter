# MiroFish A2 "Brain"

An **isolated, loopback-only** equity-analysis micro-service. It takes a
`(ticker, as_of, idea_fingerprint)` request, assembles point-in-time evidence
(Alpaca daily bars + SEC company-facts fundamentals), asks an LLM judge for two
independent analyst opinions (a SHORT technical-led view and a MEDIUM
fundamental-led view), and returns them over HTTP.

It is the **A2 advisor** behind the arbiter's `A2.mirofish` adapter. The arbiter
reaches it **only over loopback HTTP** — it never imports this package, and this
package never imports `arbiter`.

## Hard guarantees

- **Never 500s.** Any failure anywhere in `/analyze` degrades to a schema-valid
  `{"opinions": [], "run_id": "..."}` (abstain). The arbiter client fails closed
  on an empty body, so an outage is a safe no-op.
- **Loopback only.** The service refuses to start on a non-loopback host
  (`127.0.0.1` / `::1` / `localhost` only).
- **Point-in-time.** `as_of` is always supplied by the caller; there is no
  `datetime.now()` in the request path. Alpaca bars are filtered to `t <= as_of`
  and SEC facts to `filed <= as_of`.
- **Fresh `run_id`** (uuid4 hex) per call; a cache hit reuses opinions but NOT
  the `run_id`.
- **Negative stances pass through unchanged** (no `abs()`, no floor at 0) — a
  bearish read is a first-class signal.
- **Secrets are redacted** in `Config.__repr__` (`ANTHROPIC_API_KEY`,
  `ALPACA_SECRET_KEY` → `***`); logs never print keys.

## Endpoints

- `POST /analyze` — body `{"ticker", "as_of" (ISO-8601 UTC), "idea_fingerprint"}`,
  returns `{"opinions": [{stance_score, confidence, horizon_days, rationale,
  source_fingerprint}], "run_id"}`. `opinions` is length 0, 1, or 2.
- `GET /health` — `{"status": "ok"}`.

## Running it

Tests / the offline `--fake-llm` mode need **no API keys and no network** (a
`FakeLLM` derives a deterministic, pack-keyed opinion):

```bash
MIROFISH_FAKE_LLM=1 python -m mirofish
```

For the real LLM path, set the keys below and omit `MIROFISH_FAKE_LLM`. The
service binds `127.0.0.1:8900` by default.

### Setup keys

| Env var              | Purpose                                                        |
| -------------------- | ------------------------------------------------------------- |
| `ANTHROPIC_API_KEY`  | LLM judge (required unless `MIROFISH_FAKE_LLM=1`).             |
| `EDGAR_USER_AGENT`   | SEC fair-access User-Agent (reuse the arbiter's value).       |
| `ALPACA_API_KEY`     | Alpaca market-data key id (reuse the arbiter's value).        |
| `ALPACA_SECRET_KEY`  | Alpaca market-data secret (reuse the arbiter's value).        |
| `ALPACA_DATA_FEED`   | Optional; default `iex`.                                      |
| `MIROFISH_MODEL`     | Optional; default `claude-sonnet-4-6`.                        |
| `MIROFISH_HOST`      | Optional; default `127.0.0.1` (loopback only).                |
| `MIROFISH_PORT`      | Optional; default `8900`.                                     |
| `MIROFISH_CACHE_TTL_SECONDS` | Optional; default `86400` (1 day).                    |
| `MIROFISH_FAKE_LLM`  | `1` to use the deterministic offline FakeLLM.                 |

### Wiring it into the arbiter

Once the service is running on loopback, point the arbiter at it by setting in
`arbiter/.env`:

```
MIROFISH_ENDPOINT=http://127.0.0.1:8900
```

The arbiter's `A2.mirofish` adapter then calls `/analyze` over loopback. A2 runs
in **shadow mode** (weight 0 in fusion) until it earns a track record.

## The `valuation_z` heuristic (honest caveat)

A2's only outbound hosts are SEC and Alpaca — there is **no live peer-P/E
cross-section** reachable. So `valuation_z` (this name's P/E vs its sector) is
derived from a **vendored static sector baseline** in
`mirofish/data/sector_valuation.py`: `SECTOR_MAP` (ticker → coarse sector) and
`SECTOR_PE_BASELINE` (sector → rough `(median_pe, stdev_pe)`), with
`valuation_z = (pe_ratio - median_pe) / stdev_pe`. These are hand-set,
order-of-magnitude baselines reflecting long-run sector P/E norms — a
**directional prior, not a precise market cross-section**. Treat the medium
(fundamental-led) opinion's valuation input accordingly.

## Tests

From the repo root, against the shared arbiter venv (import-isolated — a test
asserts no mirofish file imports arbiter):

```bash
arbiter/.venv/bin/python -m pytest mirofish/tests/ -q
arbiter/.venv/bin/python -m ruff check mirofish
```
