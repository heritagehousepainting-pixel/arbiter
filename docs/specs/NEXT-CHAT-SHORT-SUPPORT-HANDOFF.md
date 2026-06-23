# Next-Chat Handoff — Build SHORT SUPPORT, then redeploy

Paste the PROMPT block below into a fresh session. Everything under it is reference.

---

## PROMPT (paste into a new chat)

You are continuing work on **arbiter**, a local-first autonomous paper-trading "smart-money decision
engine" at `/Users/jonathanmorris/poly_bot/arbiter/`. It is **LIVE on a real $10k Alpaca PAPER account**
(`EXECUTOR_BACKEND=alpaca_paper`) driven by a launchd **market-hours daemon** (`com.arbiter.daemon`), with
its own A2 "MiroFish" inference service (`com.mirofish.service`, port 8900). Your memory files
(`arbiter-project`, `mirofish-a2-brain`, `user-workflow-parallel-agents`) load automatically — read the
arbiter-project memory's tail first; it has the full live history.

**Your ONE job this session: build SHORT-position support, test it, and redeploy.** Do NOT rebuild anything
else. Background: A2 (MiroFish) emits bearish stances, so the council now OPENS short positions (it already
shorted T −3 @ $22.19 and UBER −1 @ $72.19 this session). The user chose to **ALLOW SHORTING** (paper, "let
losses happen, we're testing"). But shorting is **half-built** — the system can open shorts but cannot
manage or risk-count them. Fix that.

**Always use `.venv/bin/python`.** Verify health before/after: `cd /Users/jonathanmorris/poly_bot/arbiter &&
.venv/bin/python -m pytest tests/ -q` (expect **~2360 passing**, ~50s) + `bash scripts/check_no_lookahead.sh`
+ `bash scripts/check_insert_only.sh` (both clean). MiroFish suite: `cd /Users/jonathanmorris/poly_bot &&
arbiter/.venv/bin/python -m pytest mirofish/tests -q` (96 passing). Tests MUST stay offline.

### The three defects to fix (all found live this session)
1. **`arbiter/execution/exit_monitor.py` SKIPS shorts** → they are UNMANAGED (no stop-loss, no horizon close,
   no reversal; never reach MONITORED→CLOSED; no learning outcome).
   - `~L558`: `held = [t for t, p in positions.items() if p.shares > 0]`
   - `~L579`: `if position.shares <= 0: continue`
   - `decide_exit` (`~L104`), `recompute_stop` (`~L84`), `build_sell_order` (`~L148`) are all LONG-ONLY:
     stop fires `current_price <= stop_level`; the exit order is a SELL.
   **Fix:** manage shorts too. For a short (`shares < 0`): **invert the stop** — fire when
   `current_price >= avg_price × (1 + stop_fraction)` (a short loses as price rises); **cover via a BUY**
   (not a SELL — a SELL would enlarge the short); **reversal** fires on a BULLISH fresh opinion
   (`current_stance >= +reversal_threshold`, i.e. the mirror of the long rule); **horizon** unchanged. Thread
   the position side through `decide_exit`/`recompute_stop` and make the exit-order builder emit BUY-to-cover
   for shorts with the absolute share count. Keep all the existing long behavior identical.
2. **`arbiter/engine/safety_ops.py::position_market_value` (`~L173`)** returns SIGNED `shares × price`, so a
   short REDUCES measured gross exposure → the risk book under-counts short risk (over-allocation).
   **Fix:** use **`abs()`** so a short's market value counts toward gross/limits. Check the call site
   `_seed_risk_book` (`~L200`) and `decide()`/`sizing.py` for any place that needs the signed value vs the
   exposure magnitude — gross/open-count want magnitude; don't break long behavior.
3. **Reconciler is already correct** for shorts (`_local_positions` = BUY−SELL net, handles negatives) — no
   change needed, but add a test asserting a short position reconciles clean.

### Tests (offline; add one per fix)
- exit monitor: a short position whose price ROSE past the inverted stop → fires `stop_loss` and the built
  order is a **BUY** for `abs(shares)`; a short with a BULLISH fresh opinion → `reversal`; horizon for a
  short → `horizon`; a profitable short (price dropped) → NO exit. Keep the existing long tests green.
- risk book: a short position contributes `abs(market_value)` to gross.
- reconciler: a `-3` local net matches a `-3` broker position (clean).

### Deploy (after the suite + linters are green)
The daemon caches code at startup, so restart it to load the fix:
`launchctl kickstart -k "gui/$(id -u)/com.arbiter.daemon"`. Then verify it picks up the existing shorts and
manages them (heartbeat `is_open=True/fast/paused=False`; exit monitor now iterates T/UBER). The user's
existing two shorts (T, UBER) should stay open and become managed.

### SAFETY / how the user works (honor this)
- **To halt during the build, use the KILL SWITCH** (set the Cloudflare Worker var `HALTED=true` at
  `https://stockbot.heritagehousepainting.workers.dev`). The daemon's **self-heal RESPECTS the kill switch**
  but would AUTO-RESUME a plain durable pause — so do NOT rely on `engine` pause to stop it. Ask the user to
  flip the kill switch if you want trading stopped while you build (you cannot flip their Cloudflare).
- This is risk-critical execution code on a LIVE (paper) account — change behavior carefully, keep the full
  suite green, run plan→build→verify. The user is fine with paper losses but NOT with broken machinery.
- The user spawns batches of parallel agents for big work and likes plan→audit→build loops with disjoint
  file ownership; push back on wasteful agent counts. This build is small enough to do directly.
- Save any plan to `docs/specs/`, log audits to `docs/audit/`. Confirm you've read the materials and
  summarize the plan before editing.

Start by: confirming the live state (`.venv/bin/python -m arbiter.cli status`; the daemon + mirofish launchd
services; current broker positions), then implement the three fixes, test, and redeploy.

---

## Reference — current live state at handoff (2026-06-22 ~13:00 ET)
- `EXECUTOR_BACKEND=alpaca_paper`, $10k paper account, equity ~$9,993.
- Positions: **AMZN +1** (long, MONITORED, ~−$6.5), **T −3** (short, UNMANAGED), **UBER −1** (short, UNMANAGED).
- Open order: **NVDA buy 1 @ $207.52** (limit not hit).
- Daemon `com.arbiter.daemon` LIVE (KeepAlive), hourly entry cycles
  `ARBITER_FULL_CYCLE_TIMES_ET=09:35,10:00,11:00,12:00,13:00,14:00,15:00,15:30` — **will keep opening UNMANAGED
  shorts each slot until the fix ships.** Market closes 16:00 ET.
- MiroFish `com.mirofish.service` LIVE on 127.0.0.1:8900 (real Claude; `ANTHROPIC_API_KEY` set).
- Kill switch live (`{"halted":false}`); alerts → ntfy `arbiter-alerts-REDACTED`.
- DB backups: `data/arbiter.db.{pre-golive,pre-ideacleanup,pre-rerun,pre-hourly}-bak`.
- 3 live bugs already fixed this session (sub-penny limit tick, `get_order` client_order_id endpoint, market
  calendar pre-open cache) — all regression-tested. **2360 tests green.**

## Reference — verification commands
- `cd /Users/jonathanmorris/poly_bot/arbiter && .venv/bin/python -m pytest tests/ -q`
- `bash scripts/check_no_lookahead.sh` && `bash scripts/check_insert_only.sh`
- `cd /Users/jonathanmorris/poly_bot && arbiter/.venv/bin/python -m pytest mirofish/tests -q`
- daemon: `launchctl list | grep arbiter.daemon`; `tail -f arbiter/data/arbiter-daemon.stdout.log`
- redeploy: `launchctl kickstart -k "gui/$(id -u)/com.arbiter.daemon"`
