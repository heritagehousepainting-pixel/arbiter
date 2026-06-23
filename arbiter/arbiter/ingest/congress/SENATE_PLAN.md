# Senate eFD Ingestion Adapter — Build Plan

Verified live 2026-06-19. Mirrors REBUILD_PLAN.md; adds Senate as a second chamber that emits the same
`RawFiling` output via `to_raw_filings`. House = primary. Senate = best-effort, non-blocking.

---

## 1. Access status (verified)

| Endpoint | Method | Status | Notes |
|---|---|---|---|
| `GET /search/home/` | GET | 200 — live | Django app; returns agreement form + CSRF |
| `POST /search/home/` | POST | 200 — live | Agree to terms → sets `sessionid` cookie |
| `POST /search/report/data/` | POST AJAX | **503 (Akamai maintenance)** when called with wrong Content-Type or wrong payload; **200 — live** with correct DataTables payload | See §3 |
| `GET /search/view/ptr/{uuid}/` | GET | 200 — live | Returns full HTML transaction table |
| `GET /search/view/paper/{uuid}/` | GET | 200 — live | Scanned PDF viewer page — SKIP |

**Summary**: The site IS accessible from this environment with no IP blocking. The 503 observed earlier
was caused by sending an incorrect payload format to the AJAX endpoint; the correct DataTables format
works. The live test returned 80 PTR records for 2026.

---

## 2. Authentication flow (CSRF + session)

Three-step flow, must be done in sequence in the same HTTP session:

### Step 1 — GET home page
```
GET https://efdsearch.senate.gov/search/home/
User-Agent: Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) ...Chrome/125...
```
Response sets two cookies:
- `csrftoken` — a 32-char random string; used as the CSRF token value in subsequent requests
- `33a5c6d97f299a223cb6fc3925909ef7` — an anonymous session/fingerprint cookie

Also extracts from the HTML form:
```html
<input type="hidden" name="csrfmiddlewaretoken" value="<64-char-token>">
```
The `csrfmiddlewaretoken` in the form body differs from the `csrftoken` cookie (double-submit CSRF pattern).

### Step 2 — POST agreement
```
POST https://efdsearch.senate.gov/search/home/
Content-Type: application/x-www-form-urlencoded
Referer: https://efdsearch.senate.gov/search/home/
X-CSRFToken: <csrftoken cookie value>
Origin: https://efdsearch.senate.gov

prohibition_agreement=1&csrfmiddlewaretoken=<form token from step 1>
```
Response: `200 OK`, title `eFD: Find Reports`. Sets a new cookie:
- `sessionid` — Django session cookie that marks the user as having agreed to terms

**All subsequent requests must include the `sessionid` cookie.** Use a persistent `httpx.Client` so
cookies are automatically propagated.

### Step 3 — POST search (AJAX)
```
POST https://efdsearch.senate.gov/search/report/data/
X-CSRFToken: <current csrftoken cookie value>
X-Requested-With: XMLHttpRequest
Accept: application/json, text/javascript, */*; q=0.01
Content-Type: application/x-www-form-urlencoded; charset=UTF-8
Referer: https://efdsearch.senate.gov/search/
```
**Exact payload fields** (from reverse-engineering the DataTables JS in the search page):
```
draw=1
start=0                           # pagination offset
length=100                        # page size (max observed: 100)
search[value]=
search[regex]=false
order[0][column]=4
order[0][dir]=desc
report_types=[11]                 # 11 = Periodic Transactions (PTR); 7=Annual, 10=Extension
filer_types=[1]                   # 1 = Senator; 4 = Candidate
submitted_start_date=01/01/2026 00:00:00   # note: SPACE + time, not just date
submitted_end_date=12/31/2026 23:59:59
candidate_state=
senator_state=
office_id=
first_name=
last_name=
```
Response: JSON with DataTables envelope.

---

## 3. Search response shape

```json
{
  "draw": 1,
  "recordsTotal": 80,
  "recordsFiltered": 80,
  "result": "ok",
  "data": [
    ["John", "Boozman", "Boozman, John (Senator)",
     "<a href=\"/search/view/ptr/a9754ff5-901a-4877-b7be-a647bd361c52/\" target=\"_blank\">Periodic Transaction Report for 06/16/2026</a>",
     "06/16/2026"],
    ...
  ]
}
```

Each row is `[first_name, last_name, office_label, report_link_html, date_filed_str]`.

**Parsing the row:**
- `first_name` = `row[0]`, `last_name` = `row[1]` — may have stray spaces (e.g. `"RICHARD  BLUMENTHAL"`)
- `office_label` = `row[2]` — e.g. `"Boozman, John (Senator)"`, `"(Candidate)"` — always Senator for filer_type=1
- `report_link_html` = `row[3]` — extract UUID from `href="/search/view/(ptr|paper)/{uuid}/"`:
  - `ptr` path → electronic PTR (parse)
  - `paper` path → scanned PDF (skip)
  - Label may include `"(Amendment N)"` — capture is_amendment from label
- `date_filed_str` = `row[4]` — `"MM/DD/YYYY"` — this is the disclosure/filing date

**Electronic vs paper filter:**
```python
import re
m = re.search(r'/search/view/(ptr|paper)/([a-f0-9-]{36})/', row[3])
if not m:
    continue  # skip unparseable links
is_paper = m.group(1) == 'paper'
uuid = m.group(2)
is_amendment = bool(re.search(r'Amendment', row[3]))
```

---

## 4. PTR HTML page structure

URL: `GET https://efdsearch.senate.gov/search/view/ptr/{uuid}/`
Requires: `sessionid` cookie from the agreement flow.
Response: Full Django-rendered HTML page (no JS required for data — transactions are server-side rendered).

### Filer metadata (from `<main>`)
```html
<h1 class="mb-2">Periodic Transaction Report for MM/DD/YYYY</h1>
<h2 class="filedReport">The Honorable John Boozman (Boozman, John)</h2>
<p class="muted">
  <strong class="noWrap"><i ...></i> Filed MM/DD/YYYY @ HH:MM AM</strong>
</p>
```
- Filer name: strip `"The Honorable "` and `"Mr. "` and `"Ms. "` from the `<h2>` text
- Date filed: from the `<h1>` text or the `"Filed ..."` string in `<strong class="noWrap">`
  - **This date = notification_date** (the disclosure date; analogous to the House's `FilingDate`)

### Transaction table
```html
<table class="table table-striped">
  <thead>
    <tr class="header">
      <th>#</th>
      <th>Transaction Date</th>
      <th>Owner</th>
      <th>Ticker</th>
      <th>Asset Name</th>
      <th>Asset Type</th>
      <th>Type</th>
      <th>Amount</th>
      <th>Comment</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>1</td>
      <td>05/21/2026</td>
      <td>Self</td>
      <td><a href="https://finance.yahoo.com/quote/KHC" target="_blank">KHC</a></td>
      <td>The Kraft Heinz Company - Common Stock</td>
      <td>Stock</td>
      <td>Purchase</td>
      <td>$1,001 - $15,000</td>
      <td>--</td>
    </tr>
```

**Column mapping → Transaction fields:**

| HTML column | Transaction field | Notes |
|---|---|---|
| `#` (col 0) | `txn_idx` | Row number (1-based); use 0-based enumerate for accession |
| `Transaction Date` (col 1) | `txn_date` | `MM/DD/YYYY` → `date` |
| `Owner` (col 2) | `owner` | `"Self"→"SELF"`, `"Joint"→"JT"`, `"Spouse"→"SP"`, `"Child"→"DC"` |
| `Ticker` (col 3) | `ticker` | Text of `<a>` tag if present; `"--"` or empty → `None` |
| `Asset Name` (col 4) | `asset_name` | Strip whitespace; may include embedded ticker `"ACN - Accenture..."` |
| `Asset Type` (col 5) | `asset_type` | `"Stock"→"ST"`, `"Municipal Security"→"MS"`, `"Other Securities"→"OT"` — see §5 |
| `Type` (col 6) | `txn_type` | `"Purchase"→"P"`, `"Sale (Full)"→"S"`, `"Sale (Partial)"→"S" + is_partial=True`, `"Exchange"→"E"` |
| `Amount` (col 7) | `amount_low`, `amount_high` | `"$1,001 - $15,000"` → parse both numbers |
| `Comment` (col 8) | ignored | Usually `"--"` |

**Notification date** comes from the page `<h1>` / `<h2>`, NOT from a column in the row.
Every transaction in a single PTR page shares the same `notification_date` (the filing date).

### Amount parsing
```python
import re
_AMOUNT_RE = re.compile(r'\$\s*([\d,]+)\s*-\s*\$\s*([\d,]+)')
m = _AMOUNT_RE.search(amount_cell)
amount_low = float(m.group(1).replace(',', ''))
amount_high = float(m.group(2).replace(',', ''))
```

### Ticker extraction
```python
ticker_tag = td_cols[3]  # raw HTML
ticker_match = re.search(r'<a\b[^>]*>([^<]+)</a>', ticker_tag)
ticker = ticker_match.group(1).strip() if ticker_match else None
if ticker in ('--', '', None):
    ticker = None
```

---

## 5. Asset type normalisation

The Senate HTML uses long-form strings; normalise to the same two-letter codes used in `Transaction.asset_type`:

| Senate HTML | Code | Keep? |
|---|---|---|
| `Stock` | `ST` | YES |
| `Other Securities` | `OT` | YES (includes ETFs) |
| `Municipal Security` | `MS` | NO (drop — no ticker) |
| `Corporate Bond` | `CS` | NO |
| `Government Security` | `GS` | NO |
| `Hedge Fund` | `HN` | NO |
| `Real Property` | `RP` | NO |
| any unknown | `OT` | YES |

The existing `normalize.to_raw_filings` already drops `GS`, `MS`/`CS`/`HN`/`RP` via `_DROP_ASSET_TYPES`.
The Senate adapter normalises the HTML string to the two-letter code BEFORE passing to `to_raw_filings`.

---

## 6. `Transaction` contract for Senate rows

Senate `Transaction` objects use the same frozen dataclass from `ptr_pdf.py`. Mapping:

```python
Transaction(
    doc_id    = uuid,                          # the PTR UUID (replaces numeric DocID)
    chamber   = "senate",
    member_name = member_name,                  # "John Boozman" (no title)
    owner     = owner_code,                     # SELF|JT|SP|DC (see §4)
    asset_name = asset_name,                    # from col 4
    ticker    = ticker_or_none,                 # from col 3 (None if --)
    asset_type = normalised_asset_type_code,    # from col 5 (see §5)
    txn_type  = "P" | "S" | "E",               # from col 6
    is_partial = bool,                          # True if "Sale (Partial)"
    txn_date  = txn_date_date,                  # from col 1
    notification_date = filing_date_date,       # from page <h1>/<h2>
    amount_low  = float,
    amount_high = float,
)
```

---

## 7. Accession scheme (idempotency)

Senate accession = `f"S-{uuid}-{i}"` where `i` is the 0-based input enumerate position
(mirrors the House scheme `"H-{doc_id}-{i}"`).

Rationale: Senate UUIDs are stable identifiers; they replace the numeric House DocID.
Using INPUT position (not post-filter position) means re-runs with different filter
sets do not change accessions of surviving transactions.

---

## 8. Amendment handling

Senate PTRs may be amendments (the `report_link_html` includes `"(Amendment N)"`).
- The `is_amendment` flag is set at the `RawFiling` level to `True` when detected.
- In `normalize.to_raw_filings`, the current code always sets `is_amendment=False`
  at the transaction level (per REBUILD_PLAN.md rule 10). The Senate adapter
  sets this at the `RawFiling` construction step rather than in `Transaction`.
- **Dedup behaviour**: if both the original and an amendment are ingested, both
  get different UUIDs and different `accession` values. The writer's dedup key is
  `accession`, so both are written. The downstream MEDIUM-tier signal consumer
  should prefer the amendment (most recent) — this is out of scope for the adapter.

---

## 9. Paper PTR handling

Rows with `/search/view/paper/{uuid}/` in the link → scanned document, no machine-readable data.
**Skip entirely**. Log at DEBUG level:
```
senate: skipping paper PTR {uuid} for {first} {last} (filed {date})
```

---

## 10. Pagination

The search API returns `recordsTotal`. Paginate by incrementing `start` in steps of `length`:
```python
page_size = 100
start = 0
while True:
    resp = _post_search(start=start, length=page_size, ...)
    yield from resp['data']
    if start + page_size >= resp['recordsTotal']:
        break
    start += page_size
```

---

## 11. File ownership for the BUILD agent

| File | Owner | What to build |
|---|---|---|
| `senate.py` (NEW) | Senate agent | Entire Senate flow: CSRF flow, search pagination, HTML parsing, `Transaction` assembly. Public API: `fetch_senate_ptrs(year, http_client) -> list[Transaction]`. Imports `Transaction` from `ptr_pdf.py` and `to_raw_filings` from `normalize.py`. |
| `client.py` | Senate agent (senate methods only) | Replace `fetch_senate_index` + `fetch_senate_ptrs` stubs with real implementations that delegate to `senate.py`. Leave House methods untouched. |
| `normalize.py` | NO CHANGES | `to_raw_filings` already handles `chamber="senate"` correctly. The only required change is the accession prefix: `f"S-{doc_id}-{i}"` for senate vs `f"H-{doc_id}-{i}"` for house — parameterise by chamber. |
| `ptr_pdf.py` | NO CHANGES | `Transaction` dataclass is reused as-is. |
| `__init__.py` / runner glue | Integration agent | Wire `CongressClient.fetch_senate_ptrs` → `senate.py` → `to_raw_filings` into the ingest run. |
| `tests/ingest/test_senate.py` (NEW) | Senate agent | Offline tests using fixtures from `tests/ingest/fixtures/congress/senate/`. |

### New module: `senate.py`

```python
# arbiter/ingest/congress/senate.py
"""Senate eFD ingestion — Layer S (HTTP + HTML parsing, returns list[Transaction]).

Flow:
  1. GET /search/home/ → capture csrftoken cookie + csrfmiddlewaretoken form field.
  2. POST /search/home/ with prohibition_agreement=1 → sets sessionid cookie.
  3. POST /search/report/data/ with DataTables payload → JSON list of PTR links.
  4. Filter: electronic (/ptr/) only; skip paper (/paper/).
  5. GET /search/view/ptr/{uuid}/ → parse HTML table → list[Transaction].

Public API:
  fetch_senate_ptrs(year, http_client=None) -> list[Transaction]
"""
```

Internal helpers in `senate.py`:
- `_do_agreement_flow(client) -> str`  — steps 1-2, returns current csrf token
- `_search_ptrs(client, csrf, year) -> list[dict]`  — step 3, paginated
- `_parse_ptr_page(html, uuid) -> list[Transaction]`  — step 5, pure function
- `_parse_filer_name(h2_text) -> str`  — strip titles
- `_parse_notification_date(h1_text) -> date`  — "...for MM/DD/YYYY"
- `_parse_owner(cell) -> str`  — "Self"→"SELF" etc.
- `_parse_asset_type(cell) -> str | None`  — "Stock"→"ST", drop-list etc.
- `_parse_ticker(cell_html) -> str | None`  — extract from `<a>` or return None for `"--"`
- `_parse_amount(cell) -> tuple[float, float]`  — "$N - $M"
- `_parse_txn_type(cell) -> tuple[str, bool]`  — ("P"|"S"|"E", is_partial)

---

## 12. Accession prefix change in `normalize.py`

Currently `to_raw_filings` hardcodes `f"H-{doc_id}-{i}"`. Add a `chamber` param:

```python
def to_raw_filings(
    transactions: list[Transaction],
    *,
    chamber_prefix: str | None = None,  # "H" for house, "S" for senate; inferred from txn.chamber if None
) -> list[RawFiling]:
    ...
    prefix = chamber_prefix or ("S" if txn.chamber == "senate" else "H")
    accession = f"{prefix}-{doc_id}-{i}"
```

This is the ONLY required change to `normalize.py`. All filter rules are chamber-agnostic.

---

## 13. Test strategy (offline, using fixtures)

Fixtures in `tests/ingest/fixtures/congress/senate/`:

| Fixture | Content | Tests |
|---|---|---|
| `search_result_ptrs_2026.json` | 5 rows from real 2026 search | `_search_ptrs` parsing: UUID extraction, paper/electronic filter, amendment flag |
| `ptr_a9754ff5_boozman_2026.html` | Boozman 06/16/2026: 18 Joint sales, all ETFs/stocks | Full `_parse_ptr_page`: 18 transactions, correct ticker extraction, `JT` owner, `Sale (Partial)` |
| `ptr_be9bb561_peters_2026.html` | Peters 06/11/2026: 1 Self purchase of KHC | Single purchase → `P` txn_type, `SELF` owner, `ST` asset_type |
| `ptr_09b9c1ed_king_2026.html` | King 03/24/2026: 9 Spouse sales of individual equities | Multi-row, `SP` owner, UBER/PYPL/ONON/NFLX/MSFT tickers |

All tests mock the HTTP layer; zero real network in tests.

---

## 14. Fallback plan (if site becomes inaccessible)

The live test on 2026-06-19 succeeded. But if the Akamai CDN blocks automated access in the future:

1. **Return empty + log clearly**: `fetch_senate_ptrs` raises `SenateEFDUnavailable` (subclass of `CongressFetchError`); the runner catches it, logs `WARNING senate_efd_unavailable`, and continues with House-only data.
2. **Tests remain offline**: The existing fixture-based tests cover the parser logic completely; a live network failure does not break CI.
3. **Rate limit**: The site has no stated rate limit. Empirically: 80 PTR pages × 1 GET each per year → well within safe bounds. Add a 0.5 s sleep between PTR page fetches as a courtesy.
4. **Session expiry**: The `sessionid` cookie from the agreement POST may expire mid-run. If any PTR page returns a redirect back to the home/agreement page (detectable by `title == "eFD: Home"` or `title == "eFD: Find Reports"` when expecting `"eFD: Print Periodic Transaction Report"`), re-run the agreement flow and retry once.

---

## 15. The exact search/report/data payload that was confirmed live

```python
{
    "draw": "1",
    "start": "0",
    "length": "100",
    "search[value]": "",
    "search[regex]": "false",
    "order[0][column]": "4",
    "order[0][dir]": "desc",
    "report_types": "[11]",           # PTR only; string representation of JSON array
    "filer_types": "[1]",             # Senator only
    "submitted_start_date": "01/01/2026 00:00:00",
    "submitted_end_date": "12/31/2026 23:59:59",
    "candidate_state": "",
    "senator_state": "",
    "office_id": "",
    "first_name": "",
    "last_name": "",
}
```
Key discoveries:
- `report_types` NOT `report_type` (plural).
- The value is a JSON array string `"[11]"`, not the integer `11`.
- Dates include `" HH:MM:SS"` suffix (space-separated, not `T`).
- The earlier 503 was a red herring: the Akamai maintenance page was served because the
  endpoint saw wrong Content-Type before CSRF validation; with correct DataTables form-encoded
  payload + `X-Requested-With: XMLHttpRequest`, the Django app responds 200.

---

## 16. Verified sample data (from live 2026 run)

- 80 total PTR records for senators in 2026 YTD.
- ~34 paper PTRs (mostly Blumenthal); ~46 electronic PTRs.
- Active electronic filers: Boozman (12 PTRs), Fetterman (5), Whitehouse (4), Capito (4), McCormick (5), Peters (2), King (1), Hickenlooper (2), Collins (2), Moreno (1), Mullin (1), McConnell (1), Smith (2), Rounds (1), Banks (2), Hagerty (1).

Typical transaction density per PTR: 1–37 transactions. McCormick has the densest (37 rows),
mostly municipal bonds and structured notes (all filtered by `to_raw_filings` → no equity signal).
