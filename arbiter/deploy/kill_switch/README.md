# Arbiter Kill Switch — deploy (pick ONE)

The engine does `GET <KILL_SWITCH_URL>` before every cycle and expects
**`200 {"halted": true|false}`**. Anything else (error, non-200, bad JSON,
unreachable) is treated as **halted=true** — it **fails closed**, so a broken
kill switch stops trading rather than risking it.

`halted: false` = trading permitted. `halted: true` = block all new orders.

You host this on something **off-box** (so it keeps working even if the
arbiter machine dies). Three easy options, fastest control first:

---

## Option A — Cloudflare Worker  *(recommended: instant flip, free)*
1. Go to <https://workers.cloudflare.com> → **Create Worker**.
2. Replace the code with `worker.js` from this folder → **Deploy**.
3. Copy the Worker URL (e.g. `https://arbiter-ks.<you>.workers.dev`). That's your `KILL_SWITCH_URL`.
4. **To halt later:** Worker → Settings → Variables → add `HALTED = true` → Deploy. Flip is near-instant.

## Option B — GitHub Gist  *(zero hosting, ~1 min CDN lag on flips)*
1. Go to <https://gist.github.com> → new **secret** gist.
2. Filename `kill.json`, contents exactly:  `{"halted": false}`
3. Create → click **Raw** → copy that URL. That's your `KILL_SWITCH_URL`.
   (It looks like `https://gist.githubusercontent.com/<you>/<id>/raw/kill.json`.)
4. **To halt later:** edit the gist to `{"halted": true}` and save. Note: GitHub's
   CDN can take up to ~a minute to serve the change — fine for v1, but Option A is
   snappier for a true emergency stop.

## Option C — Val.town  *(instant, free, tiny)*
1. <https://val.town> → new HTTP val. Body:
   ```ts
   export default async () => Response.json({ halted: false });
   ```
2. Copy the val's HTTP endpoint URL → that's your `KILL_SWITCH_URL`.
3. To halt: change `false` → `true`, save (instant).

---

## After you have the URL
Send it to me (or paste into `arbiter/.env` as `KILL_SWITCH_URL=...`) and I'll
wire it + verify the engine reads `halted:false` and that flipping to `true`
actually halts a cycle — before any go-live.
