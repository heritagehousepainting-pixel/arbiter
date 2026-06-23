# Task 12 Report — Cockpit A1.fund Advisor Node

## Status
DONE — all gates green.

## Changed Files

### `cockpit/api/graph.py`
5 additions:
1. `_DATA_SOURCES`: added `("src.form13f", "SEC 13F (fund managers)")` after `src.form13d`.
2. `_ADVISORS`: added `("A1.fund", "A1 · Funds", "form13f")` after A1.activist. Rendered live/un-dimmed (`future=False`) because all advisors in `build_graph` receive `meta={"future": False}` — no special flag needed.
3. `_SOURCE_TO_ADVISOR`: added `"src.form13f": "A1.fund"`.
4. `_FILING_SOURCE_TO_NODE`: added `"form13f": "src.form13f"`.
5. `_FIGURE_KIND`: added `"form13f": "fund manager"`.

### `cockpit/api/events.py`
- `_VALID_ADVISORS` frozenset: added `"A1.fund"` (line ~57). Mirrors A1.activist treatment.

### `cockpit/api/state.py`
- `_advisor_intensities` cold-start loop (line ~148): added `"A1.fund"` to the tuple of known advisors that must appear (dim if no data).
- `_data_source_intensities` `source_to_node` dict (line ~161): added `"form13f": "src.form13f"` so 13F filing volume drives `src.form13f` node intensity.

### `cockpit/api/test_api.py`
Appended two new tests (section `(f)`):
- `test_graph_includes_a1_fund_node` — asserts `A1.fund` in `/graph` nodes with `future` in `(False, None)`.
- `test_graph_includes_src_form13f_node` — asserts `src.form13f` in `/graph` nodes.

## Web Touched?
No. `cockpit/web/src/contract.ts` does not enumerate advisor IDs (only node type/cluster unions), so no web changes were needed.

## Test Results

### Cockpit API suite
```
88 passed, 1 warning in 14.39s
```
(baseline 86 → 88, +2 new tests)

### Web tsc
```
(clean — no output, exit 0)
```

### Web vitest
```
Test Files  5 passed (5)
Tests  67 passed (67)
```

## Deviations
None. Implemented exactly as specified.

## Concerns
None. The `A1.fund` node is un-dimmed from day one because all advisor nodes are built with `meta={"future": False}` in `build_graph`. The state liveness loop ensures it appears at cold-start intensity (0.05) before any 13F filings arrive.
