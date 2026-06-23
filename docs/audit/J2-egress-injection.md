# J2 — Egress Controls, Injection & Untrusted-Input Surface

**Auditor lane:** J2 (read-only)
**Date:** 2026-06-19
**Scope:** MiroFish egress firewall, SQL building, untrusted-input ingest parsers (Congress PDF/HTML, Senate eFD HTML, EDGAR/Alpaca JSON+XML), and the web dashboard. Secrets are out of scope (J1).
**Threat model:** A locally-run, single-operator trading bot that pulls remote data over HTTP. There is no multi-tenant boundary and no hostile authenticated user. The realistic adversary is (a) a malicious/compromised upstream data source (SEC, Senate eFD, Alpaca, FMP, Simfin) returning crafted payloads, and (b) an attacker who can set environment variables (which already means full code execution — game over).

---

## VERDICT

**No P0 or P1 findings.** The injection and egress surface is unusually disciplined for this codebase. SQL building is safe (table names and column keys are hardcoded literals; all values are `?`-bound). The MiroFish egress firewall is sound for its stated purpose (A2/A3 independence) but has redirect and SSRF gaps that are P2/P3 given the local-only threat model. Untrusted-input parsers use stdlib `xml.etree` (no XXE), stdlib `HTMLParser`, and `pdfplumber` — no `eval`/`exec`/`pickle`/`yaml.load`/`extractall` anywhere. The web dashboard is read-only, bound to 127.0.0.1, and escapes all dynamic output. The main residual risks are unbounded in-memory reads of remote bytes (zip-bomb / memory exhaustion) and the egress firewall not being applied to redirects.

---

## FINDINGS

### P2 — httpx follows redirects without re-checking egress — `arbiter/adapters/mirofish/http_client.py:149,165`
`check_egress(url)` validates only the *initial* URL. `httpx.post(url, ...)` is called with default redirect behavior. While httpx defaults to `follow_redirects=False` (so a redirect returns a 3xx rather than being chased), this is an implicit guarantee, not an enforced one — a future refactor to `follow_redirects=True`, or a switch to a client configured otherwise, would let a malicious/compromised allowed host (or the self-hosted MiroFish endpoint) 302-redirect egress to an arbitrary host, fully bypassing the allowlist and the A2/A3 independence contract that this module exists to enforce.
**Why it matters:** The entire point of `egress.py` is to be the *single enforcement point*. A redirect is an enforcement hole that the design doc does not acknowledge.
**Recommended action:** Pass `follow_redirects=False` explicitly to `httpx.post`, OR use an httpx event hook / transport that calls `check_egress` on every request URL including redirects. Add a test asserting a 302 to a blocked host raises rather than follows.

### P2 — No size cap on remote bytes read fully into memory (zip-bomb / memory exhaustion) — `arbiter/ingest/congress/index.py:62-92`, `arbiter/ingest/congress/ptr_pdf.py:71`, `arbiter/ingest/edgar/client.py`
`parse_index` does `io.BytesIO(zip_bytes)` then `zf.read(target)` on a single named member with no decompressed-size limit; a crafted `{year}FD.txt` member could decompress to gigabytes (classic zip bomb) and OOM the process. Similarly `pdfplumber.open(io.BytesIO(pdf_bytes))` and the EDGAR client read full response bodies into memory with no `Content-Length`/byte cap anywhere in the ingest layer (grep for size caps returned only retry counts and lag-day limits, no byte limits).
**Why it matters:** A compromised or spoofed upstream (or a MITM on plain HTTP) can crash the bot — a denial-of-service on a system that is supposed to be making time-sensitive trading decisions. This is the most realistic remote-payload attack here.
**Recommended action:** Enforce a max decompressed size when reading zip members (read in bounded chunks, abort past a threshold e.g. 50 MB), and cap HTTP response body size in the EDGAR/Senate/Alpaca clients. The `ptr_pdf` path already wraps parsing in try/except and never raises, so a size guard there is cheap.

### P3 — MiroFish localhost enforcement allows any local port; SSRF to internal services possible — `arbiter/adapters/mirofish/egress.py:64-67`
`localhost`, `127.0.0.1`, and `::1` are allowlisted with no port restriction. `MIROFISH_ENDPOINT` is operator-controlled, so this is only reachable by someone who already controls the environment (i.e. not a meaningful boundary). But if a future code path ever lets a *remote* value influence the MiroFish URL, the localhost allowance becomes an SSRF primitive against any loopback-bound service (other dashboards, metadata-style local agents). Note also `0.0.0.0`, `[::]`, and `127.0.0.2`/`127.x.x.x` variants are NOT in the list but `127.0.0.1`/`::1` cover the common case; an attacker-supplied `http://127.0.0.1.nip.io` style host would be rejected by the allowlist (good).
**Why it matters:** Low under the current threat model (endpoint is env-controlled), but the localhost allowance is the one place the firewall is permissive.
**Recommended action:** If feasible, pin the expected MiroFish port in the allowlist check (host+port), or document that `MIROFISH_ENDPOINT` must never be derived from remote/untrusted data. Keep the allowlist loopback-only.

### P3 — Egress allowlist is host-only; an allowed host's news/press endpoints are reachable (independence drift) — `arbiter/adapters/mirofish/egress.py:58-79`
`financialmodelingprep.com` and `simfin.com` are allowlisted at the *host* level, but the module's own docstring warns their `/news` and `/press-releases` paths must NOT be used. The firewall checks only the hostname, so nothing technically prevents MiroFish from hitting `api.financialmodelingprep.com/v3/stock_news` — the A2/A3 independence guarantee degrades to a code-review convention, not an enforced control. The `_BLOCKED_KEYWORDS` list catches hostnames containing "news"/"sentiment"/etc., but a *path* like `/news` on an allowed host slips through (keywords are matched against `host`, not the full URL).
**Why it matters:** This is the specific allowlist-drift / independence risk called out in the task. It is the firewall's stated reason to exist, and it is enforced only at host granularity.
**Recommended action:** Either (a) extend `check_egress` to also scan `parsed.path` against `_BLOCKED_KEYWORDS`, or (b) maintain a per-host path allowlist for FMP/Simfin. At minimum, document that the host-only check does not enforce the path-level independence rule.

### P3 — Billion-laughs / entity-expansion DoS theoretically possible in EDGAR XML — `arbiter/ingest/edgar/parser.py:146`, `arbiter/ingest/edgar/client.py:214`
Parsing uses stdlib `xml.etree.ElementTree.fromstring`. Modern CPython's ElementTree does **not** resolve external entities (no XXE / no SSRF-via-DTD — confirmed: no lxml, no custom resolver), so the classic file-read / SSRF XXE is not exploitable. However, internal entity expansion ("billion laughs") is not fully defused by stdlib ElementTree and could in principle exhaust memory if SEC EDGAR returned a malicious Atom/Form-4 document.
**Why it matters:** Very low — the source is SEC over HTTPS and the payloads are well-formed Form 4 / Atom. Listed for completeness.
**Recommended action:** If hardening is desired, parse with `defusedxml.ElementTree` (drop-in) for the two EDGAR call sites. Otherwise accept the risk given the trusted source.

---

## NON-FINDINGS (verified safe — do not re-flag)

- **`db/helpers.py:58,88` `INSERT INTO {table} ({columns})`** — SAFE. Every caller passes a hardcoded string-literal table name (`"filings"`, `"opinions"`, `"orders"`, `"outcomes"`, `"trust_weights"` — verified at all 8 call sites). The `{columns}` are dict keys, but every `row` dict is constructed with hardcoded literal keys in source (e.g. `writer.py` builds `{"amount_high": ..., "is_10b5_1": 0, ...}`); column names are NEVER derived from parsed/untrusted data. All values use `?` placeholders. No injection path.
- **`db/helpers.py:133,174` `UPDATE {table} SET is_superseded = 1 WHERE {pk_col} = ?`** — SAFE. `table` is a literal; `pk_col` comes from the hardcoded `_pk_column` override map; `old_id` is `?`-bound.
- **`outcome_store.py:199`, `opinion_store.py:151`, `idea_store.py:211` `SELECT ... {where}` / `IN ({placeholders})`** — SAFE. WHERE clauses are assembled from static column-name string literals; every value is appended to `params` and bound with `?`. `placeholders` is `", ".join("?" ...)`, not data.
- **`db/migrate.py:54` `PRAGMA table_info({table})`** — SAFE. `table` originates from migration code with literal table names, not external input.
- **No `eval`/`exec`/`pickle`/`marshal`/`yaml.load`/`os.system`/`subprocess`/`shell=True`** anywhere in `arbiter/` (grep-confirmed).
- **Zip handling** uses `zf.read(name)` on a single resolved member — NO `extractall`, no path-traversal surface.
- **Senate eFD HTML** parsed with stdlib `html.parser.HTMLParser` — no entity resolution, no SSRF.
- **Web dashboard (`web/server.py`)** — read-only (only `do_GET`, zero mutating routes), bound to `127.0.0.1` (bind host hardcoded; `--host` flag explicitly ignored), all dynamic values pass through `html.escape` via `_e()` (orders, breakers, audit payloads), JSON payloads serialized then escaped. Path is split on `?` and matched against a fixed allowlist (`/`, `/health`), else 404. No SSRF (server makes no outbound calls), no injection sink found. `Cache-Control: no-store` set.

---

## OPPORTUNITIES TO ADD

1. **Make `egress.check_egress` redirect-proof by construction.** Wrap MiroFish (and ideally all ingest HTTP) in a shared httpx client with `follow_redirects=False` and an event hook that runs the allowlist on every request URL. This converts the egress firewall from "checked once at the call site" to "enforced on every byte that leaves the process."

2. **Centralize a bounded HTTP fetch helper** (`max_bytes`, hard timeout, no redirects, explicit allowlist) and route EDGAR / Senate / Alpaca / Congress-index downloads through it. Today each client reads full bodies into memory independently with no byte cap — a single helper closes the zip-bomb / OOM class in one place.

3. **Promote the path-level independence rule into the firewall.** Add `parsed.path` to the `_BLOCKED_KEYWORDS` scan (or per-host path allowlists) so "no FMP/Simfin news endpoints" is enforced, not just commented.

4. **Swap the two EDGAR `ET.fromstring` calls for `defusedxml`** as cheap defense-in-depth against entity-expansion DoS, even though stdlib already blocks external-entity XXE.

5. **Add a regression test** that feeds `insert_row` a `row` dict whose key contains `");DROP TABLE` and asserts it is rejected or treated as a literal column (it will currently produce a SQL error, which is acceptable — but a test documents the invariant that callers must control keys).
