# StockBot Retirement / Arbiter Audit Map

Status date: 2026-06-19

This note is a read-only inventory for deciding what Arbiter should mine from
StockBot before StockBot is removed. Nothing has been deleted.

## Keep-Side Context

Primary folders the user wants to keep:

- `/Users/jonathanmorris/poly_bot/arbiter` - active Arbiter project.
- `/Users/jonathanmorris/poly_bot/docs` - shared handoff/spec docs.
- `/Users/jonathanmorris/poly_bot/polybot` - older/smaller Polybot project.

Parent-folder disk usage observed:

- `arbiter`: 362M
- `docs`: 60K
- `polybot`: 320K
- `stockbot-pead`: 33M
- `stockbot-ui`: 5.0M
- `stockbot`: 57G

The apparent `poly_bot` size problem is therefore StockBot-specific.

## Why StockBot Is Huge

`/Users/jonathanmorris/poly_bot/stockbot` is about 57G because
`/Users/jonathanmorris/poly_bot/stockbot/.git` is about 57G.

Visible working files are small by comparison:

- `stockbot/data`: 74M
- `stockbot/src`: about 2.5M
- `stockbot/specs`: about 200K
- `stockbot/tests`: about 492K

Git object-store findings:

- `git count-objects -vH` reported 88,696 loose objects.
- Loose object size: 56.54 GiB.
- Packfiles: 0.
- Size garbage: 5.95 MiB.
- `git rev-list --objects --all | wc -l` reported only 212 reachable objects.
- 16,584 loose object files over 1M account for about 45.08 GiB.
- 2,658 loose object files over 4M account for about 11.16 GiB.
- Loose object timestamps cluster heavily on 2026-06-11 and 2026-06-12.

Interpretation: most of the 57G appears to be unreachable loose Git blobs from
repeated staging/checkpointing of generated research/data files, not active
working-tree content. Do not run prune/GC until the user explicitly approves
cleanup, because unreachable objects are still a last-resort recovery source.

## Git Worktree Coupling

StockBot has three linked worktrees:

- `/Users/jonathanmorris/poly_bot/stockbot` on branch `main`, HEAD `4dd16c2`.
- `/Users/jonathanmorris/poly_bot/stockbot-pead` on branch `pead`, HEAD `4dd16c2`.
- `/Users/jonathanmorris/poly_bot/stockbot-ui` on branch `ui`, HEAD `87f146a`.

The PEAD and UI directories point their `.git` files back to:

- `/Users/jonathanmorris/poly_bot/stockbot/.git/worktrees/stockbot-pead`
- `/Users/jonathanmorris/poly_bot/stockbot/.git/worktrees/stockbot-ui`

Deleting or moving `stockbot/.git` later will break those two worktrees unless
they are separately preserved first.

## Not Safe To Discard Yet

Do not blindly delete these before Arbiter audits them:

- Uncommitted `stockbot` source/spec/test additions for options, operator, Alpaca
  paper execution, experiment registry, overnight gap, TSMOM, and options
  dashboard.
- Uncommitted `stockbot-pead` PEAD research lane:
  - `src/pead_research.py`
  - `tests/test_pead_research.py`
  - `specs/pead-research/`
  - `data/quant/pead_report.*`
- Uncommitted `stockbot-ui` dashboard work:
  - `src/web/server.py`
  - `src/web/static/dashboard.css`
  - `src/web/static/dashboard.js`
  - `src/web/templates/dashboard.html`
- `.env` files, because they contain set credential variables. Do not copy them
  into docs or Git. Treat as local secrets only.
- `operator/.env`, because it has a set `MINIMAX_API_KEY`.
- `data/experiments/registry.jsonl` and `data/operator/audit.jsonl`, if Arbiter
  wants provenance of prior operator/experiment runs.

Main worktree uncommitted diff is large mostly because CSV logs changed:

- `data/price_snapshots.csv`
- `data/signals.csv`
- `data/portfolio_snapshots.csv`

These are useful for forensic context, but much less strategically valuable than
the source/spec/test files.

## Safe / Low-Value Cleanup Candidates After Audit

Only after Arbiter has finished mining useful parts, these are likely safe
cleanup candidates:

- `__pycache__/` folders and `*.pyc`.
- `.DS_Store` files.
- Generated dashboard JSON under `data/options/`.
- Generated reports and Parquet/CSV research outputs under `data/quant/`, if the
  corresponding source/tests/specs are preserved.
- Large CSV runtime logs under `data/*.csv`, if no longer needed for audit.
- The 57G loose Git object store, via an explicit user-approved Git cleanup or
  by archiving/copying the useful working trees elsewhere before removing
  StockBot.

## High-Value Code To Mine For Arbiter

Execution and safety:

- `src/executor.py`
- `src/sim_executor.py`
- `src/alpaca_paper_executor.py`
- `src/executor_factory.py`
- `src/risk.py`
- `src/cost_model.py`
- `tests/test_executor.py`

Operator/orchestration:

- `src/operator_engine.py`
- `src/operator_mcp_server.py`
- `operator/README.md`
- `specs/operator-layer.md`
- `tests/test_operator_layer.py`

Experiment tracking:

- `src/experiment_registry.py`
- `tests/test_experiment_registry.py`
- `data/experiments/registry.jsonl`

Research diagnostics:

- `src/research.py`
- `src/quant_tracer.py`
- `src/daily_research.py`
- `src/edge_map.py`
- `src/overnight_gap.py`
- `src/tsmom.py`
- `stockbot-pead/src/pead_research.py`
- `specs/quant-research/00-invariants.md`
- `stockbot-pead/specs/pead-research/`
- `tests/test_quant_tracer.py`
- `tests/test_edge_map.py`
- `tests/test_daily_research.py`
- `tests/test_overnight_gap.py`
- `tests/test_tsmom.py`
- `stockbot-pead/tests/test_pead_research.py`

Options/paper strategy lane:

- `src/options_engine.py`
- `src/options_market_data.py`
- `src/options_dashboard.py`
- `src/put_credit_spread.py`
- `src/wheel_csp.py`
- `src/pmcc.py`
- `src/iron_condor_0dte.py`
- `src/put_ratio_backspread.py`
- `src/orb_0dte.py`
- `src/long_directional_control.py`
- `specs/options-main-lane/`
- `specs/options-dashboard-contract.md`
- `specs/options-research/strategy-knowledge.md`
- `tests/test_options_engine.py`
- `tests/test_put_credit_spread.py`
- `tests/test_wheel_csp.py`
- `tests/test_pmcc.py`
- `tests/test_iron_condor_0dte.py`
- `tests/test_put_ratio_backspread.py`
- `tests/test_orb_0dte.py`
- `tests/test_long_directional_control.py`

Dashboard lane:

- `stockbot-ui/src/web/server.py`
- `stockbot-ui/src/web/static/dashboard.css`
- `stockbot-ui/src/web/static/dashboard.js`
- `stockbot-ui/src/web/templates/dashboard.html`

## Safety Flags Observed

Variable names only were inspected for `.env` files; credential values were not
printed into this note.

- `stockbot/.env`: `EXECUTOR_MODE=alpaca_paper`, `ALPACA_PAPER_BASE_URL=https://paper-api.alpaca.markets`.
- `stockbot-pead/.env`: `EXECUTOR_MODE=sim`.
- `stockbot-ui/.env`: `EXECUTOR_MODE=sim`.
- All three: `ENABLE_ROBINHOOD_MCP=false`.
- All three: `ROBINHOOD_MCP_SCHEMA_VERIFIED=false`.
- All three: `ROBINHOOD_LIVE_CALLS_ENABLED=false`.
- All three: `LIVE_TRADING=false`.

StockBot main is paper-account capable, not real-money live enabled. Arbiter
should still treat all broker-adjacent code as safety-sensitive and keep
execution behind its own policy/executor boundaries.

## Suggested Arbiter Audit Order

1. Read StockBot's `AGENTS.md`, `README.md`, `status.md`, and `specs/README.md`.
2. Mine execution boundary code first, especially executor interfaces and tests.
3. Mine experiment registry and operator audit patterns.
4. Mine quant invariants and leakage tests, not generated reports first.
5. Mine options strategy code only as deterministic paper research examples.
6. Review PEAD and UI worktrees separately because they contain uncommitted work.
7. Decide what source/spec/test files to copy into Arbiter or docs.
8. Only then decide whether to archive/delete StockBot and reclaim the 57G Git
   object store.

