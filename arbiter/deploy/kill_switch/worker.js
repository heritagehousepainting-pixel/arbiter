// Arbiter kill switch — Cloudflare Worker (instant flip, free tier).
// Deploy: https://workers.cloudflare.com  →  Create Worker  →  paste this  →  Deploy.
// The Worker URL it gives you (e.g. https://arbiter-ks.<you>.workers.dev) is your KILL_SWITCH_URL.
//
// To HALT trading: set the Worker env var HALTED=true (Settings → Variables) and redeploy,
// OR edit the constant below and redeploy. Engine fails closed, so HALT also happens
// automatically if this endpoint ever errors or is unreachable.

export default {
  async fetch(request, env) {
    const halted = (env.HALTED ?? "false").toLowerCase() === "true";
    return new Response(JSON.stringify({ halted }), {
      status: 200,
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  },
};
