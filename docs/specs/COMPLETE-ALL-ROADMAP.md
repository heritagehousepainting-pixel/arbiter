# Arbiter — "Complete Everything" Roadmap

Status start: **2026-06-19**. Baseline at plan start: **2088 tests green**, `EXECUTOR_BACKEND=sim`.
Driver: `/goal` autonomous execution. User decisions locked (see below).

> **Current status (post-build):** Waves 1 + 2 built. Full suite now **2335 tests green** (offline);
> still `EXECUTOR_BACKEND=sim` / not live (no autonomous flip per locked decision #1). The Wave-2
> `engine.py` refactor landed — `arbiter/engine.py` is now the `arbiter/engine/` package
> (`_engine.py` + `advisors.py` + …), so references below to the single-file `engine.py`
> "god-object" describe the pre-refactor state. EDGAR (Form-4 + 13D/G) and MiroFish are wired but
> **inert until `EDGAR_USER_AGENT` / `MIROFISH_ENDPOINT` are set** (see SETUP_NEEDED.md).

## Locked decisions (from user, this session)
1. **Go-live (#5):** build everything, **STOP before the flip.** No autonomous flip to
   `alpaca_paper`. Deliver a checklist; user flips manually after sign-off + safety URLs.
2. **EDGAR (#1, #4b):** build Form-4 + 13D/13G fully, offline-test; **inert until the user
   sets `EDGAR_USER_AGENT`.** → record in SETUP_NEEDED.md.
3. **MiroFish (#2):** harden client + contract tests + configured-or-noop wiring; **endpoint
   not up → mark honestly NOT-DONE**, inert until `MIROFISH_ENDPOINT` set.
4. **Agents:** right-sized (~4 domains), NOT 25. One agent per independent domain, disjoint
   file ownership, plan→audit→build waves.

## Hard constraint: `engine.py` is a single-owner resource
Advisors plug into `arbiter/engine.py` via `_build_a1_insider_fn` / `_build_a1_congress_fn`.
Form-4, 13D/13G, MiroFish-wiring, AND the refactor all touch `engine.py`. Therefore **all
`engine.py` edits serialize into Wave 2 under one owner.** Wave-1 agents touch only their
ingest/adapter/data layer and expose a documented wiring contract.

## Waves

### Wave 1 — parallel, disjoint, zero `engine.py`
| Domain | Owns | Tasks |
|---|---|---|
| **D1 EDGAR** | `arbiter/ingest/edgar/**`, `tests/ingest/edgar/**` | Fix Form-4 discovery (#1); add 13D/13G parser+normalize (#4b) |
| **D2 MiroFish** | `arbiter/adapters/mirofish/**`, `tests/adapters/mirofish/**` | Harden client + contract tests (#2) |
| **D3 P2** | `arbiter/data/sectors.py` + isolated P2 files | Sector-map breadth, notional-vs-realized fold, minor P2s (#4c) |

Each Wave-1 domain MUST end its spec with a **WIRING CONTRACT** section: the exact
function signature(s) `engine.py` will call, so Wave 2 builds against a frozen interface.

### Wave 2 — serialized, single owner of `engine.py`
| Domain | Owns | Tasks |
|---|---|---|
| **D4 engine** | `arbiter/engine.py` (+ extracted modules) | Wire D1/D2 advisors against frozen contracts (#1/#4b/#2), THEN refactor 1698-line god-object (#4a) with frozen public interface (INTERFACES.md) |

### Post-build (orchestrator, no agents)
- **#3** run `arbiter backfill` + verify learning loop on accumulated data.
- **Audit ×2** (function / wording / layout / security) per goal skill §4.
- **#5** go-live **checklist only** → SETUP_NEEDED.md. **No flip.**

## Invariants every agent must honor
- Use `.venv/bin/python`. Tests MUST stay OFFLINE (no network, no real sleeps).
- After each wave the orchestrator runs the FULL suite + `scripts/check_no_lookahead.sh`
  + `scripts/check_insert_only.sh` — all must stay green/clean.
- Each agent runs ONLY its targeted tests during build; orchestrator runs the full suite.
- Disjoint file ownership — never edit another domain's files.
- Honest status: simulated/inert/not-done is stated plainly, never dressed up as live.
