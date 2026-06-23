"""Read-only HTTP dashboard server for Arbiter (Lane 5 / web).

Serves a live status dashboard at ``/`` (HTML) and a health-check at
``/health`` (JSON 200).  All other routes return 404.

The dashboard is dependency-free (stdlib only) and defensive: a missing table,
empty DB, or absent audit.jsonl renders gracefully rather than 500-ing.

Design rules (INTERFACES.md §11):
  - No datetime.now() — time values come from DB rows and audit.jsonl only.
  - Read-only: zero mutating routes.
  - Fail-closed: defaults to SIM/paper mode; the banner is always honest.

Public API
----------
build_server(config, port) -> ThreadingHTTPServer
    Create a server bound to 127.0.0.1 on *port*.

serve(port) -> None
    Load config, build, and block in serve_forever().

CLI wiring
----------
Intended to be invoked as ``arbiter dashboard --port 8798`` via cli.py.
Can also run standalone: ``python -m arbiter.web.server --port 8798``.
"""
from __future__ import annotations

import argparse
import html
import json
import sqlite3
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from arbiter.config import Config, load_config
from arbiter.runtime.health import HealthMonitor
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.db.audit import read_audit
from arbiter.safety.breakers import BREAKER_NAMES
from arbiter.signals.leaderboard import render_leaderboard
from arbiter.web.queries import (
    get_advisor_count,
    get_all_breakers,
    get_recent_orders,
    get_tripped_breakers,
)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8798

# Maximum audit lines to tail for the dashboard panel.
_AUDIT_TAIL = 20


# ---------------------------------------------------------------------------
# HTML helpers
# ---------------------------------------------------------------------------

def _e(text: object) -> str:
    """HTML-escape *text* (for safe injection into HTML)."""
    return html.escape(str(text))


def _badge(text: str, *, ok: bool) -> str:
    colour = "#2d8a4e" if ok else "#c0392b"
    return (
        f'<span style="background:{colour};color:#fff;padding:2px 8px;'
        f'border-radius:4px;font-size:0.85em;font-weight:bold">'
        f"{_e(text)}</span>"
    )


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------

def _render_sim_live_banner(live_trading: bool) -> str:
    if live_trading:
        style = (
            "background:#c0392b;color:#fff;padding:12px 20px;"
            "border-radius:6px;font-size:1.2em;font-weight:bold;"
            "text-align:center;margin-bottom:20px"
        )
        label = "LIVE TRADING — real orders, real money"
    else:
        style = (
            "background:#2471a3;color:#fff;padding:12px 20px;"
            "border-radius:6px;font-size:1.2em;font-weight:bold;"
            "text-align:center;margin-bottom:20px"
        )
        label = "SIM / PAPER mode — no real orders"
    return f'<div style="{style}">{label}</div>'


def _render_orders_section(conn: sqlite3.Connection) -> str:
    rows = get_recent_orders(conn, limit=20)
    lines = [
        "<section>",
        "<h2>Paper Positions / Recent Orders</h2>",
    ]
    if not rows:
        lines.append('<p style="color:#666"><em>No orders recorded yet.</em></p>')
    else:
        lines.append(
            '<table border="1" cellspacing="0" cellpadding="6" '
            'style="border-collapse:collapse;width:100%">'
        )
        lines.append(
            "<thead><tr>"
            "<th>Order ID</th><th>Ticker</th><th>Side</th>"
            "<th>Qty</th><th>Bucket</th><th>Entry Date</th>"
            "<th>Advisor</th><th>Status</th>"
            "</tr></thead><tbody>"
        )
        for r in rows:
            status = str(r["status"])
            status_ok = status.upper() in ("FILLED", "OPEN", "ACTIVE")
            lines.append(
                "<tr>"
                f"<td><code>{_e(r['order_id'][:12])}…</code></td>"
                f"<td><strong>{_e(r['ticker'])}</strong></td>"
                f"<td>{_e(r['side'])}</td>"
                f"<td>{_e(r['qty'])}</td>"
                f"<td>{_e(r['horizon_bucket'])}</td>"
                f"<td>{_e(r['entry_date'])}</td>"
                f"<td><code>{_e(r['advisor_signature'][:20])}</code></td>"
                f"<td>{_badge(status, ok=status_ok)}</td>"
                "</tr>"
            )
        lines.append("</tbody></table>")
    lines.append("</section>")
    return "\n".join(lines)


def _render_leaderboard_section(config: Config) -> str:
    """Render the A1 leaderboard using render_leaderboard(plain=True)."""
    # We need an as_of datetime.  Use the epoch as a sentinel when we have
    # no real clock — this matches the INTERFACES.md §11 rule (no datetime.now()).
    # The leaderboard uses as_of only for display; cold-start scores are static.
    from datetime import datetime, timezone
    _EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

    try:
        leaderboard_text = render_leaderboard(_EPOCH, plain=True)
    except Exception as exc:  # noqa: BLE001
        leaderboard_text = f"(leaderboard unavailable: {exc})"

    return (
        "<section>"
        "<h2>A1 Signal Leaderboard</h2>"
        f'<pre style="background:#f4f4f4;padding:16px;border-radius:4px;'
        f'overflow-x:auto;font-size:0.87em">{_e(leaderboard_text)}</pre>'
        "</section>"
    )


def _render_safety_section(conn: sqlite3.Connection) -> str:
    tripped = get_tripped_breakers(conn)
    all_breakers = get_all_breakers(conn)
    advisor_count = get_advisor_count(conn)

    # Determine degradation level from advisor count (quorum rule §8).
    if advisor_count >= 2:
        deg_level = "NORMAL"
        deg_ok = True
    elif advisor_count == 1:
        deg_level = "DEGRADED (1 advisor)"
        deg_ok = False
    else:
        deg_level = "HALTED (0 advisors)"
        deg_ok = False

    # Known breaker names for completeness when breaker_state is empty.
    known_names = sorted(BREAKER_NAMES)

    lines = [
        "<section>",
        "<h2>Safety Status</h2>",
        f"<p>Degradation level: {_badge(deg_level, ok=deg_ok)}</p>",
        f"<p>Registered advisors: <strong>{_e(advisor_count)}</strong></p>",
        "<h3>Circuit Breakers</h3>",
    ]

    if all_breakers:
        # Render from DB state.
        lines.append(
            '<table border="1" cellspacing="0" cellpadding="6" '
            'style="border-collapse:collapse;width:100%">'
        )
        lines.append(
            "<thead><tr>"
            "<th>Breaker</th><th>State</th><th>Latched At</th><th>Reason</th>"
            "</tr></thead><tbody>"
        )
        for r in all_breakers:
            latched = bool(r["latched"])
            state_label = "TRIPPED" if latched else "OK"
            lines.append(
                "<tr>"
                f"<td><code>{_e(r['breaker_name'])}</code></td>"
                f"<td>{_badge(state_label, ok=not latched)}</td>"
                f"<td>{_e(r['latched_at'] or '—')}</td>"
                f"<td>{_e(r['reason'] or '—')}</td>"
                "</tr>"
            )
        lines.append("</tbody></table>")
    else:
        # DB has no rows: show the canonical names with OK status.
        lines.append(
            '<table border="1" cellspacing="0" cellpadding="6" '
            'style="border-collapse:collapse">'
        )
        lines.append(
            "<thead><tr><th>Breaker</th><th>State</th></tr></thead><tbody>"
        )
        for name in known_names:
            lines.append(
                "<tr>"
                f"<td><code>{_e(name)}</code></td>"
                f"<td>{_badge('OK', ok=True)}</td>"
                "</tr>"
            )
        lines.append("</tbody></table>")

    if tripped:
        lines.append(
            f'<p style="color:#c0392b"><strong>'
            f"{len(tripped)} breaker(s) TRIPPED — trading may be restricted."
            f"</strong></p>"
        )

    lines.append("</section>")
    return "\n".join(lines)


def _render_audit_section(audit_path: str) -> str:
    records = read_audit(audit_path)
    tail = records[-_AUDIT_TAIL:] if records else []

    lines = [
        "<section>",
        f"<h2>Recent Audit Events (last {_AUDIT_TAIL})</h2>",
    ]
    if not tail:
        lines.append('<p style="color:#666"><em>No audit events yet.</em></p>')
    else:
        lines.append(
            '<table border="1" cellspacing="0" cellpadding="6" '
            'style="border-collapse:collapse;width:100%">'
        )
        lines.append(
            "<thead><tr><th>Timestamp</th><th>Event</th><th>Payload</th></tr></thead>"
            "<tbody>"
        )
        for rec in reversed(tail):  # newest first
            payload_str = json.dumps(rec.get("payload", {}), default=str)
            lines.append(
                "<tr>"
                f"<td><code>{_e(rec.get('ts', '?'))}</code></td>"
                f"<td><code>{_e(rec.get('event', '?'))}</code></td>"
                f"<td><pre style='margin:0;font-size:0.8em'>{_e(payload_str)}</pre></td>"
                "</tr>"
            )
        lines.append("</tbody></table>")
    lines.append("</section>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Full page render
# ---------------------------------------------------------------------------

_PAGE_STYLE = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       max-width: 1100px; margin: 0 auto; padding: 20px; color: #222; }
h1   { border-bottom: 2px solid #333; padding-bottom: 8px; }
h2   { color: #2c3e50; margin-top: 32px; }
h3   { color: #555; }
section { margin-bottom: 40px; }
table { font-size: 0.9em; }
th    { background: #eee; }
code  { background: #f0f0f0; padding: 1px 4px; border-radius: 3px; }
"""


def _render_dashboard(config: Config, conn: sqlite3.Connection) -> bytes:
    """Build the full dashboard HTML and return as UTF-8 bytes."""
    banner = _render_sim_live_banner(config.live_trading)
    orders = _render_orders_section(conn)
    leaderboard = _render_leaderboard_section(config)
    safety = _render_safety_section(conn)
    audit = _render_audit_section(config.audit_path)

    mode_label = "LIVE" if config.live_trading else "SIM/PAPER"
    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Arbiter Dashboard [{_e(mode_label)}]</title>
  <style>{_PAGE_STYLE}</style>
</head>
<body>
  <h1>Arbiter Decision Engine</h1>
  {banner}
  {orders}
  {leaderboard}
  {safety}
  {audit}
</body>
</html>"""
    return html_doc.encode("utf-8")


# ---------------------------------------------------------------------------
# Health payload (B-HEALTH)
# ---------------------------------------------------------------------------

def compute_health_payload(config: Config, *, now: datetime | None = None) -> dict:
    """Compute the live ``/health`` payload from the durable artifacts.

    Replaces the old hardcoded ``{"status": "ok"}`` literal: a stale heartbeat,
    paused engine, zero-row ingest, or high fallback-proxy rate all flip
    ``healthy`` to False with reasons, so a comatose bot no longer reads green.

    ``now`` is injected by tests; in production it comes from the sanctioned
    live ``Clock`` (heartbeat *age* is undefined without a real "now") — never a
    raw ``datetime.now()``, which the no-look-ahead lint forbids outside clock.py.
    """
    if now is None:  # pragma: no cover - exercised only by the live server
        from arbiter.data.clock import Clock  # noqa: PLC0415

        now = Clock().now()
    monitor = HealthMonitor.from_config(config)
    return monitor.health_report(now)


# ---------------------------------------------------------------------------
# Handler factory
# ---------------------------------------------------------------------------

def _create_handler(
    config: Config,
    db_path: str,
) -> type[BaseHTTPRequestHandler]:
    """Return a BaseHTTPRequestHandler class wired to *config* and *db_path*."""

    class ArbiterDashboardHandler(BaseHTTPRequestHandler):
        server_version = "ArbiterWeb/1.0"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass  # suppress default access log; structlog handles it

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            status: HTTPStatus = HTTPStatus.OK,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]  # strip query string

            if path in ("/", ""):
                self._handle_index()
            elif path == "/health":
                self._handle_health()
            else:
                self.send_error(HTTPStatus.NOT_FOUND, "Not Found")

        def _handle_index(self) -> None:
            try:
                conn = get_connection(db_path)
                try:
                    body = _render_dashboard(config, conn)
                finally:
                    conn.close()
            except Exception as exc:  # noqa: BLE001
                # Last-resort: render a minimal error page rather than 500.
                error_html = (
                    "<!DOCTYPE html><html><body>"
                    f"<h1>Arbiter Dashboard Error</h1><pre>{_e(str(exc))}</pre>"
                    "</body></html>"
                )
                body = error_html.encode("utf-8")
            self._send_bytes(body, "text/html; charset=utf-8")

        def _handle_health(self) -> None:
            # Real health: reads heartbeat / metrics / audit / DB via the
            # HealthMonitor.  Returns 503 when unhealthy so external monitors
            # (and an HTTP healthcheck) trip instead of reading a green literal.
            try:
                report = compute_health_payload(config)
            except Exception as exc:  # noqa: BLE001 - never 500 the healthcheck
                report = {
                    "healthy": False,
                    "reasons": [f"health monitor error: {exc}"],
                }
            status = (
                HTTPStatus.OK if report.get("healthy")
                else HTTPStatus.SERVICE_UNAVAILABLE
            )
            payload = json.dumps(report).encode("utf-8")
            self._send_bytes(
                payload, "application/json; charset=utf-8", status=status
            )

    return ArbiterDashboardHandler


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_server(config: Config, port: int) -> ThreadingHTTPServer:
    """Create and return a ThreadingHTTPServer bound to 127.0.0.1:*port*.

    The server is NOT started; call ``server.serve_forever()`` to block.

    Args:
        config: Loaded Arbiter Config (drives live_trading, db_path, audit_path).
        port:   TCP port to listen on.

    Returns:
        A configured ThreadingHTTPServer instance.
    """
    db_path = config.db_path

    # Run migrations so the dashboard always works against a current schema.
    # Defensive: if migrations fail (e.g. read-only FS) we log but don't crash.
    try:
        conn = get_connection(db_path)
        run_migrations(conn)
        conn.close()
    except Exception:  # noqa: BLE001
        pass  # Dashboard renders empty sections — not a fatal error.

    handler_cls = _create_handler(config, db_path)
    server = ThreadingHTTPServer((DEFAULT_HOST, port), handler_cls)
    return server


def serve(port: int = DEFAULT_PORT) -> None:
    """Load config, build the server, and block in serve_forever().

    This is the entry point wired by ``arbiter dashboard --port``.

    Args:
        port: TCP port (default 8798).
    """
    config = load_config()
    server = build_server(config, port)
    mode = "LIVE" if config.live_trading else "SIM/paper"
    print(f"Arbiter dashboard [{mode}] running at http://{DEFAULT_HOST}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping Arbiter dashboard.")
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Arbiter read-only dashboard")
    parser.add_argument(
        "--host", default=DEFAULT_HOST, help="bind host (ignored; always 127.0.0.1)"
    )
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
