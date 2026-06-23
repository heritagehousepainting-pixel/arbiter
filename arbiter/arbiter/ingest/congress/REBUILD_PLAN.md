# Congress adapter rebuild — official House zip-index + PTR-PDF pipeline

The old `client.py` fetched `{year}FD.json` (404 — doesn't exist) and assumed a single JSON
endpoint. This rebuilds the adapter against the REAL, verified official source. Frozen contracts
below let the 5 layers be built in parallel without colliding. Output `RawFiling` is unchanged.

## Verified live endpoints (all return 200 as of 2026-06-19)
- **House annual index (zip):** `https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip`
  → contains `{year}FD.txt` (TAB-delimited, header row) + `{year}FD.xml`. Columns:
  `Prefix  Last  First  Suffix  FilingType  StateDst  Year  FilingDate  DocID`.
  **`FilingType == "P"` = Periodic Transaction Report** (the stock trades). 2026 had 259.
- **House PTR PDF:** `https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{DocID}.pdf`
  - **Electronic** filings = 8-digit numeric DocID (e.g. `20034201`) → **text-extractable** (pdfplumber). PARSE these.
  - **Scanned/paper** = short numeric DocID (e.g. `8068`) → image PDF, no text. SKIP + flag (no OCR in v1).
- **Senate eFD:** `https://efdsearch.senate.gov/search/` — POST-based search behind a CSRF token +
  "agree to terms" cookie. House is primary; Senate = best-effort client OR a documented stub returning
  `[]` with a clear TODO (the design demotes Congress to a slow MEDIUM signal, so House-only is acceptable v1).

Test fixtures (REAL data) live in `tests/ingest/fixtures/congress/`:
`2026FD_index_sample.txt`, `ptr_20033751.pdf`/`.txt` (Allen: FERG buy, NFLX sale, SP owner),
`ptr_20034201.pdf`/`.txt` (Alford: AMZN/AAPL/T/BRK.B partial sales, multi-page).

## Frozen intermediate contracts (every layer builds against these)
```python
@dataclass(frozen=True)
class IndexRecord:
    chamber: str          # "house" | "senate"
    doc_id: str
    filing_type: str      # "P" for PTR
    member_last: str; member_first: str; member_suffix: str
    state_dist: str       # e.g. "MO04"
    filing_date: date
    year: int
    is_electronic: bool   # True if doc_id is the 8-digit electronic form

@dataclass(frozen=True)
class PtrText:
    doc_id: str; chamber: str; year: int
    raw_text: str         # pdfplumber-extracted full text ("" if scanned/unparseable)
    is_electronic: bool

@dataclass(frozen=True)
class Transaction:
    doc_id: str; chamber: str
    member_name: str           # "Mark Alford" (strip "Hon. ")
    owner: str                 # "SP" | "DC" | "JT" | "SELF"
    asset_name: str
    ticker: str | None         # from "(TICKER)" before the [ST]/[OP] tag; None if no ticker
    asset_type: str | None     # "ST" stock, "OP" option, etc.
    txn_type: str              # "P" purchase | "S" sale | "E" exchange
    is_partial: bool
    txn_date: date
    notification_date: date    # the DISCLOSURE date (information timestamp)
    amount_low: float; amount_high: float
```
`RawFiling` (EXISTING, frozen — see `arbiter/ingest/edgar/normalize.py` for the shape) is the final output:
`source="congress"`, `ticker`, `person_name=member_name`, `filing_ts = notification_date` (tz-aware UTC ISO —
this is the ~45-day-lagged disclosure date that lands Congress in the MEDIUM bucket), `txn_type` "P"/"S",
`shares=None`, `price=None`, `amount_low`/`amount_high` (RANGES, never midpoint), `is_10b5_1=False`,
`is_amendment=False`, `accession = f"H-{doc_id}-{txn_idx}"` (synthetic, for writer dedup), `txn_idx`, `raw_json`.

## PTR text parsing rules (observed in the real fixtures)
- Member name: line `Name: Hon. <Name>` → strip `Hon. `.
- Transaction row pattern: `[<OWNER>] <asset words...> (<TICKER>) [<TAG>] <TYPE>[ (partial)] <MM/DD/YYYY> <MM/DD/YYYY> $<low> - $<high>`.
  - OWNER ∈ {SP, DC, JT}; absent ⇒ SELF.
  - The asset name + `(TICKER) [TAG]` may WRAP to the next line; the amount range may wrap too. Join continuation lines.
  - TYPE: `P`=purchase, `S`=sale, `E`=exchange; `S (partial)` ⇒ txn_type "S", is_partial=True.
  - ticker = last `(...)` group immediately before a `[XX]` tag; None if absent. Normalize `BRK/B`→`BRK.B`.
- IGNORE the repeated page-header lines: `ID Owner Asset Transaction Date Notification Amount Cap.`, `Type Date Gains >`, `$200?`,
  and the per-txn sub-lines `F S : ...`, `S O : ...`, `D : ...` (description; optional capture).

## File ownership (disjoint — no two agents edit the same file)
| Agent | Owns | Builds |
|------|------|--------|
| 1 (client) | `client.py` | HTTP: `fetch_house_index(year)->bytes`, `fetch_ptr_pdf(year,doc_id)->bytes`, Senate efd best-effort/stub. Mock network in tests. |
| 2 (index) | `index.py` | `parse_index(zip_bytes, chamber, year)->list[IndexRecord]` (unzip→txt→rows, filter FilingType=="P", set is_electronic). |
| 3 (ptr) | `ptr_pdf.py` | `extract_ptr_text(pdf_bytes,...)->PtrText` (pdfplumber) + `parse_ptr(ptr_text)->list[Transaction]`. Build/test against the 2 real fixtures. |
| 4 (normalize) | `normalize.py` | `to_raw_filings(transactions)->list[RawFiling]`; drop ticker=None + txn_type=="E"; filing_ts=notification_date; ranges; synthetic accession+txn_idx. |
| 5 (integration) | `__init__.py`, `runner` glue, `pyproject.toml` (add `pdfplumber`), end-to-end fixture test, remove/repurpose old `parser.py`, fix old congress tests | Wire layers 1→2→3→4 into `CongressClient.fetch_*`/`run_ingest`; full offline fixture e2e. |

Contracts above are the ONLY cross-layer surface. Don't import another layer's internals beyond these.
