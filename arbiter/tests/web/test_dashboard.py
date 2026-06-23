"""Tests for the Arbiter read-only dashboard (Lane 5 / web).

Constructs the HTTP handler against a temporary migrated SQLite DB (seeded
with a couple of orders, a tripped breaker, and an audit line) and asserts
correct behaviour for ``/``, ``/health``, and the empty-DB graceful path.

Design rules (INTERFACES.md §11):
  - No datetime.now() — test timestamps are hard-coded strings.
  - No network: all assertions made via direct HTTP-over-loopback using
    the stdlib http.client against a real ThreadingHTTPServer on an
    ephemeral port.
"""
from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path

import pytest

from arbiter.config import Config
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.db.audit import audit as write_audit
from arbiter.web.server import build_server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_config(db_path: str, audit_path: str, *, live_trading: bool = False) -> Config:
    """Build a minimal Config pointing at the given paths."""
    return Config(
        live_trading=live_trading,
        executor_backend="sim",
        db_path=db_path,
        audit_path=audit_path,
        metrics_path=str(Path(db_path).parent / "metrics.jsonl"),
        max_position_pct=0.05,
        max_sector_pct=0.20,
        max_gross_pct=0.80,
        max_open_positions=20,
        adv_cap_pct=0.02,
        alpaca_api_key="",
        alpaca_secret_key="",
        alpaca_paper_base_url="https://paper-api.alpaca.markets",
        alpaca_data_base_url="https://data.alpaca.markets",
        alpaca_timeout=20.0,
        edgar_user_agent="test",
        kill_switch_url="",
        alert_webhook_url="",
    )


def _seed_db(conn: "sqlite3.Connection", audit_path: str) -> None:  # noqa: F821
    """Insert two orders, one tripped breaker, and one advisor into the DB.

    All timestamps are hard-coded (no datetime.now()).
    """
    # Insert two paper orders.
    for i, (ticker, side) in enumerate([("AAPL", "BUY"), ("TSLA", "SELL")], start=1):
        conn.execute(
            """
            INSERT INTO orders
                (order_id, dedup_hash, ticker, side, qty, horizon_bucket,
                 entry_date, advisor_signature, exits_json, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"01HXX0000000000000000000{i:02d}",
                f"hash-{i:04d}",
                ticker,
                side,
                float(10 * i),
                "SHORT",
                "2026-06-18",
                "A1.insider",
                "{}",
                "OPEN",
                "2026-06-18T10:00:00+00:00",
            ),
        )

    # Trip a circuit breaker.
    conn.execute(
        """
        INSERT INTO breaker_state (breaker_name, latched, latched_at, reason)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(breaker_name) DO UPDATE SET
            latched    = excluded.latched,
            latched_at = excluded.latched_at,
            reason     = excluded.reason
        """,
        (
            "daily_loss",
            1,
            "2026-06-18T09:00:00+00:00",
            "Daily loss -2.5% breached threshold -2.0%",
        ),
    )

    # Register one advisor.
    conn.execute(
        "INSERT OR IGNORE INTO advisor_registry (advisor_id, hard_weight_cap, registered_at) VALUES (?,?,?)",
        ("A1.insider", None, "2026-06-18T00:00:00+00:00"),
    )
    conn.commit()

    # Write one audit event.
    write_audit(
        "test_event",
        {"detail": "seeded by test"},
        ts="2026-06-18T10:00:00+00:00",
        audit_path=audit_path,
    )


@pytest.fixture()
def seeded_server(tmp_path: Path):
    """Spin up a seeded dashboard server on an ephemeral port; yield (host, port)."""
    db_path = str(tmp_path / "test.db")
    audit_path = str(tmp_path / "audit.jsonl")

    conn = get_connection(db_path)
    run_migrations(conn)
    _seed_db(conn, audit_path)
    conn.close()

    config = _make_config(db_path, audit_path, live_trading=False)
    server = build_server(config, port=0)  # port=0 → OS picks ephemeral port
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def empty_server(tmp_path: Path):
    """Spin up a dashboard server with a fully migrated but empty DB."""
    db_path = str(tmp_path / "empty.db")
    audit_path = str(tmp_path / "empty_audit.jsonl")

    conn = get_connection(db_path)
    run_migrations(conn)
    conn.close()

    config = _make_config(db_path, audit_path)
    server = build_server(config, port=0)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture()
def live_server(tmp_path: Path):
    """Spin up a dashboard with live_trading=True (to test the LIVE banner)."""
    db_path = str(tmp_path / "live.db")
    audit_path = str(tmp_path / "live_audit.jsonl")

    conn = get_connection(db_path)
    run_migrations(conn)
    conn.close()

    config = _make_config(db_path, audit_path, live_trading=True)
    server = build_server(config, port=0)
    port = server.server_address[1]

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "127.0.0.1", port
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# HTTP helper
# ---------------------------------------------------------------------------

def _get(host: str, port: int, path: str = "/") -> tuple[int, str]:
    """Perform a GET request; return (status_code, body_text)."""
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    body = resp.read().decode("utf-8")
    conn.close()
    return resp.status, body


# ---------------------------------------------------------------------------
# Tests — seeded DB
# ---------------------------------------------------------------------------

class TestIndexSeeded:
    """/ with a seeded DB."""

    def test_returns_200(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        status, _ = _get(host, port, "/")
        assert status == 200

    def test_sim_banner_present(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/")
        assert "SIM" in body or "paper" in body.lower(), (
            "Dashboard must display SIM/paper mode banner"
        )

    def test_positions_section_present(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/")
        assert "Positions" in body or "Orders" in body, (
            "Dashboard must have a positions/orders section"
        )
        # Seeded tickers must appear.
        assert "AAPL" in body
        assert "TSLA" in body

    def test_leaderboard_section_present(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/")
        assert "Leaderboard" in body or "leaderboard" in body.lower(), (
            "Dashboard must have a leaderboard section"
        )

    def test_safety_section_present(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/")
        assert "Safety" in body or "Breaker" in body or "safety" in body.lower(), (
            "Dashboard must have a safety section"
        )

    def test_tripped_breaker_visible(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/")
        assert "daily_loss" in body, "Tripped daily_loss breaker must appear in dashboard"
        assert "TRIPPED" in body, "Tripped breaker must show TRIPPED state"

    def test_audit_section_present(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/")
        assert "Audit" in body or "audit" in body.lower(), (
            "Dashboard must have an audit events section"
        )
        assert "test_event" in body, "Seeded audit event must appear"


class TestHealthSeeded:
    """``/health`` with a seeded DB (B-HEALTH: real state, not a green literal)."""

    def test_returns_200_or_503(self, seeded_server: tuple[str, int]) -> None:
        # /health now reflects real state: with no live daemon heartbeat it
        # honestly reports unhealthy (503) rather than a hardcoded green 200.
        host, port = seeded_server
        status, _ = _get(host, port, "/health")
        assert status in (200, 503)

    def test_returns_json(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        _, body = _get(host, port, "/health")
        data = json.loads(body)
        assert "healthy" in data
        assert isinstance(data["reasons"], list)
        assert data["mode"] == "sim"


# ---------------------------------------------------------------------------
# Tests — empty DB
# ---------------------------------------------------------------------------

class TestEmptyDB:
    """Dashboard must render gracefully when DB has tables but no rows."""

    def test_index_returns_200(self, empty_server: tuple[str, int]) -> None:
        host, port = empty_server
        status, _ = _get(host, port, "/")
        assert status == 200

    def test_no_server_error_in_body(self, empty_server: tuple[str, int]) -> None:
        _, body = _get(*empty_server, "/")
        # Should not contain Python tracebacks or Internal Server Error.
        assert "Traceback" not in body
        assert "500" not in body

    def test_sim_banner_still_present(self, empty_server: tuple[str, int]) -> None:
        _, body = _get(*empty_server, "/")
        assert "SIM" in body or "paper" in body.lower()

    def test_health_responds(self, empty_server: tuple[str, int]) -> None:
        # /health answers (200 healthy or 503 unhealthy) without ever 500-ing,
        # even on an empty DB / no heartbeat.
        host, port = empty_server
        status, _ = _get(host, port, "/health")
        assert status in (200, 503)


# ---------------------------------------------------------------------------
# Tests — live trading banner
# ---------------------------------------------------------------------------

class TestLiveBanner:
    """When live_trading=True the banner must say LIVE, not SIM."""

    def test_live_banner(self, live_server: tuple[str, int]) -> None:
        _, body = _get(*live_server, "/")
        assert "LIVE" in body, "live_trading=True must show LIVE banner"
        assert "real" in body.lower(), (
            "LIVE banner must convey real orders / real money"
        )


# ---------------------------------------------------------------------------
# Tests — 404 on unknown route
# ---------------------------------------------------------------------------

class TestUnknownRoute:
    def test_404(self, seeded_server: tuple[str, int]) -> None:
        host, port = seeded_server
        status, _ = _get(host, port, "/nonexistent")
        assert status == 404
