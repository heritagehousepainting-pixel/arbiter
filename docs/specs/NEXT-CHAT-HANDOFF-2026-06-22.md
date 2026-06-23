# Next-Chat Handoff — Arbiter + Cockpit + A3 (as of 2026-06-22 evening)

Paste the **PROMPT** block into a fresh session. Everything under it is reference.

---

## PROMPT (paste into a new chat)

You are continuing work on **arbiter**, a local-first autonomous **$10k Alpaca PAPER** smart-money
trading engine at `/Users/jonathanmorris/poly_bot/arbiter/` (Python package is NESTED at
`arbiter/arbiter/`; **always use `.venv/bin/python`**). It runs LIVE via a launchd market-hours daemon
(`com.arbiter.daemon`), with its own A2 "MiroFish" Claude service (`com.mirofish.service`, :8900) and a
read-only 3D "Cockpit" dashboard (`/Users/jonathanmorris/poly_bot/cockpit/` — FastAPI sidecar :8910 +
Vite/React-Three-Fiber web :5173). Memory files (`arbiter-project`, `cockpit-project`,
`mirofish-a2-brain`) load automatically — **read the arbiter-project + cockpit-project memory tails
first.** The user spawns batches of parallel Sonnet agents and likes plan→audit→build; push back on
wasteful agent counts. Risk-critical live (paper) code — change carefully; keep the full suite green
(`KILL_SWITCH_URL="" ALERT_WEBHOOK_URL="" .venv/bin/python -m pytest tests/ -q` → **~2441**, ~50s) +
both linters (`bash scripts/check_no_lookahead.sh`, `bash scripts/check_insert_only.sh`). Cockpit:
`cd cockpit/web && npx tsc --noEmit && npx vitest run` (web) + `arbiter/.venv/bin/python -m pytest
cockpit/api -q` (api). To HALT trading: flip the Cloudflare kill switch `HALTED=true` (daemon
self-heal respects it). Verify cockpit via DOM measurement (`browser_evaluate`) — the Playwright
screenshot tool times out (5s cap) on the live WebGL loop. Cockpit needs **hard-refresh (Cmd+Shift+R)**
after vite restarts (cache). Confirm you've read the materials before editing.

**Live state right now:** daemon healthy, market CLOSED, sleeping until **2026-06-23 09:35 ET open**.
Positions: **AMZN long +1, UBER short −1** (equity ~$9993, daily P&L ~−$6). Kill switch = false.
**A3 (news) just went live and trades for the first time at tomorrow's open** — watch it (see below).

**Top of mind / likely next asks:**
1. **Watch A3's first live day** (tomorrow's open). A3 now spawns small 7-day-horizon paper trades
   from strong news (gated `|stance|≥0.25`, ~4 names: NVDA/UNH/TSLA/META), probationary EQUAL_FLOOR
   weight, bounded by MAX_OPEN_POSITIONS=8 / MAX_GROSS_PCT=0.50, learning-loop-suppressed if it loses.
2. **Cockpit UI tweaks (user paused mid-tweak).** OPEN: right-side panels (inspection card + "Follow
   the Money") still CLIP on the user's MacBook Air — their browser window extends past the screen's
   RIGHT edge, so right-anchored content is cut (verified the layout itself is correct at vw=1470).
   Quick dial: bump `right:48`→larger in `cockpit/web/src/ui/CockpitUI.tsx` (inspection ~L565,
   walkthrough ~L1108), or user nudges window left. Dev servers (vite :5173, api :8910) are running.
3. Optional: add the EDGAR **8-K** source as A3's 2nd corroboration feed (free, reuses EdgarClient).

---

## Reference — what this session built/changed

### 1. SHORT-position support (deployed, live-verified)
Council opens shorts via A2; exit monitor/risk/reconcile now manage them. Fixed `exit_monitor.py`
(inverted stop, BUY-to-cover, bullish reversal; `is_exit_order` discriminator), `engine/reconcile.py`
(branch on exit-vs-opening not side — was stranding shorts at FINAL_DECIDED), `safety_ops.py`
(`abs()` market value + sign-aware per-position breaker), `reconciler.py` (`abs(v)` keeps shorts).
**BONUS critical fix:** daemon self-heal re-pause loop (`runtime/daemon.py` — recovery alert went
through `_fire_critical_alert` whose AutoPauseSentinel re-paused the engine it just resumed → could
never recover). +19 short tests. Healed 2 stuck short ideas. Spec `docs/specs/2026-06-22-short-support-plan.md`.

### 2. Cockpit dashboard (BUILT this session, read-only)
`cockpit/` = true-3D **neural-constellation** map AND live ops dashboard, STRICTLY READ-ONLY (DB
opened `mode=ro`; reuses Alpaca adapter read-only; tails `data/audit.jsonl` for SSE; never writes /
never imports the engine for writes). Built foundation + 5 parallel Sonnet agents (API data, SSE
events, R3F scene, interaction, polish). Spec `docs/specs/2026-06-22-system-cockpit-design.md`,
memory [[cockpit-project]]. Then polished: edge legibility (cluster-colored, hover-trace), `FitView`
auto-center, compact 2-col legend, **Open Positions panel moved to TOP** (Dock-clip immunity),
full-screen `position:fixed inset:0` shell, "Follow the Money" walkthrough fixed to use REAL node ids
(was 404ing on `figure.pelosi`), **camera PAN** (right-click/two-finger drag), right inspection panel
→ floating card. Run: `arbiter/.venv/bin/uvicorn cockpit.api.main:app --port 8910` +
`cd cockpit/web && npm run dev`.

### 3. A3 "news" advisor (BUILT + ACTIVATED this session)
Third advisor (free news/smart-money) pushing against A1/A2. FREE stack: **Finnhub** company-news
(free key, set in `.env` as `FINNHUB_API_KEY`) + EDGAR 8-K (planned 2nd source). New
`ingest/finnhub/client.py`, `adapters/a3/{source_finnhub,stance,pipeline}.py` → `gather_a3_opinions`.
Stance = Finnhub sentiment + free keyword lexicon (NO Claude, NO new deps); diversity gate ≥2
publishers (`source_id=finnhub:<publisher>`); horizon 7d/SHORT; `as_of=clock.now()`; INERT without key
+ `[]` under BacktestClock. **AUDIT CAUGHT:** engine builds ideas ONLY from `detect_signals` (filings)
→ A3 would orphan. FIX (`engine/_engine.py` run_cycle): A3 block gathers opinions, **spawns its own
SHORT ideas**, appends them; `_gather_a3_opinions()` helper over `_DEFAULT_WATCHLIST`. NOT strict
shadow (would never learn) → **probationary EQUAL_FLOOR** (trades small, paper, learning-governed).
Cockpit `A3.news` un-dimmed. **ACTIVATED:** key added, live re-check fixed a source_id bug (Finnhub
url is its own redirect → key on the `source` publisher field), A3 emits 4 gated opinions. Then
`/goal`: added **strength gate** (`A3_MIN_STANCE=0.25`) + healed the `T -1` ledger orphan (reconciler
clean). Specs `docs/specs/2026-06-22-a3-news-advisor-spec.md` + `…-tuning-and-ledger-heal-roadmap.md`
+ research in `docs/specs/research/2026-06-22-a3-*.md`.

## Reference — running services + gotchas
- launchd: `com.arbiter.daemon` (trading), `com.mirofish.service` (A2, :8900). Restart daemon to load
  code: `launchctl kickstart -k "gui/$(id -u)/com.arbiter.daemon"`. Cockpit api/web = manual
  (uvicorn + vite, currently running).
- DB backups this session: `data/arbiter.db.{pre-shortheal,pre-tledger}-bak`.
- Test hermeticity: prefix `KILL_SWITCH_URL="" ALERT_WEBHOOK_URL=""` (the real .env has them set; the
  live switch being halted/un-halted breaks non-hermetic tests otherwise).
- `.env` loader takes the whole line after `=` (incl inline comments) → keep each value on its OWN line.
- vite binds IPv6 `[::1]` (use `curl localhost`, not `127.0.0.1`); a long HMR session can hang vite →
  `npm run dev` restart; users MUST hard-refresh after a vite restart.
- Playwright screenshot times out on the live WebGL render → verify cockpit via `browser_evaluate`
  DOM measurement + console-error checks instead.
