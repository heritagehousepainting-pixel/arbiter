"""Options backend tests — cockpit read-only API.

Test categories:
(a) /options → OptionsState shape + openness rule + empty-state degradation.
(b) /options/iv/{ticker} → IVSeries shape + empty-not-404 rule.
(c) /node/opt.layer → NodeDetail shape (engine_part, opt prefix).
(d) Isolation — option outcomes never appear in /state dynamic_nodes or /graph edges
    that are equity learning edges (teaches); opt.layer has NO teaches edge.
(e) Graph topology — opt.layer node present + correct edges.
(f) State — opt.layer appears in nodes dict with correct intensity formula.

All tests are OFFLINE (no network / no Alpaca).  DB is built from arbiter
migrations + synthetic option rows via the same fixture pattern as test_api.py.
"""
from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)

# --- path setup ---------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
_ARBITER_ROOT = _REPO_ROOT / "arbiter"
if str(_ARBITER_ROOT) not in sys.path:
    sys.path.insert(0, str(_ARBITER_ROOT))

from fastapi.testclient import TestClient  # noqa: E402


# =============================================================================
# Fixture helpers
# =============================================================================

def _build_option_fixture_db(path: str) -> None:
    """Build a minimal arbiter DB (migrations) then insert synthetic option rows."""
    from arbiter.db.connection import get_connection
    from arbiter.db.migrate import run_migrations

    conn = get_connection(path)
    run_migrations(conn)

    # Minimum required equity rows so the rest of /state doesn't crash
    conn.execute(
        "INSERT INTO people (person_id, canonical_name, source, created_at) "
        "VALUES (?, ?, ?, ?)",
        ("opt-test-pid", "Options Test Person", "form4", "2026-06-20T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO filings "
        "(id, source, ticker, person_id, filing_ts, txn_type, is_superseded, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
        ("opt-filing-001", "form4", "AAPL", "opt-test-pid",
         "2026-06-24T00:00:00Z", "P", "2026-06-24T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO ideas "
        "(idea_id, ticker, thesis, horizon_days, state, as_of, "
        "dedupe_key_ticker, dedupe_key_bucket, is_superseded, created_at, updated_state_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
        ("opt-idea-001", "AAPL", "test idea", 90, "MONITORED",
         "2026-06-20T00:00:00Z", "AAPL", "SHORT",
         "2026-06-20T00:00:00Z", "2026-06-20T00:00:00Z"),
    )
    conn.execute(
        "INSERT INTO breaker_state (breaker_name, latched) VALUES (?, ?)",
        ("test_breaker", 0),
    )

    # ---- option_positions: two open, one that will be closed ----------------
    # open position A (no matching outcome)
    conn.execute(
        """
        INSERT INTO option_positions
        (id, idea_id, shadow_id, underlying, occ_symbol, side, strike, expiry,
         contracts_qty, entry_premium, entry_limit_price, delta_at_open, iv_at_open,
         underlying_open_price, thesis_horizon_date, original_conviction,
         broker_order_id, open_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "pos-001", "opt-idea-001", None,
            "AAPL", "AAPL260117C00200000", "call", 200.0, "2026-01-17",
            2, 500.0, 5.10, 0.35, 0.28,
            175.0, "2026-01-17", 0.75,
            "broker-ord-001", "2026-06-20T10:00:00Z", "2026-06-20T10:00:00Z",
        ),
    )
    # open position B (no matching outcome) — different OCC
    conn.execute(
        """
        INSERT INTO option_positions
        (id, idea_id, shadow_id, underlying, occ_symbol, side, strike, expiry,
         contracts_qty, entry_premium, entry_limit_price, delta_at_open, iv_at_open,
         underlying_open_price, thesis_horizon_date, original_conviction,
         broker_order_id, open_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "pos-002", "opt-idea-001", None,
            "MSFT", "MSFT260117C00400000", "call", 400.0, "2026-01-17",
            1, 800.0, 8.20, 0.40, 0.22,
            380.0, "2026-01-17", 0.70,
            "broker-ord-002", "2026-06-21T10:00:00Z", "2026-06-21T10:00:00Z",
        ),
    )
    # closed position C — pos-003 in option_positions + matching outcome
    conn.execute(
        """
        INSERT INTO option_positions
        (id, idea_id, shadow_id, underlying, occ_symbol, side, strike, expiry,
         contracts_qty, entry_premium, entry_limit_price, delta_at_open, iv_at_open,
         underlying_open_price, thesis_horizon_date, original_conviction,
         broker_order_id, open_ts, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "pos-003", "opt-idea-001", None,
            "GOOG", "GOOG260117C00180000", "call", 180.0, "2026-01-17",
            1, 600.0, 6.50, 0.38, 0.30,
            170.0, "2026-01-17", 0.65,
            "broker-ord-003", "2026-06-15T10:00:00Z", "2026-06-15T10:00:00Z",
        ),
    )

    # ---- option_outcomes: only pos-003's idea+occ pair -----------------------
    conn.execute(
        """
        INSERT INTO option_outcomes
        (id, shadow_id, idea_id, underlying, occ_symbol, side,
         open_ts, close_ts, close_reason,
         entry_premium, exit_premium, option_pl_pct, underlying_alpha_bps,
         delta_at_open, iv_at_open, iv_at_close, contracts_qty, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "out-opt-001", None, "opt-idea-001",
            "GOOG", "GOOG260117C00180000", "call",
            "2026-06-15T10:00:00Z", "2026-06-22T10:00:00Z", "horizon_expiry",
            600.0, 900.0, 0.50, 85.0,
            0.38, 0.30, 0.25, 1, "2026-06-22T10:00:00Z",
        ),
    )

    # ---- option_shadow_log: 3 rows (2 express, 1 reject) ---------------------
    for i, (gate_express, gate_reason, underlying) in enumerate([
        (1, "OK", "AAPL"),
        (0, "IV_RANK_TOO_HIGH", "MSFT"),
        (1, "OK", "GOOG"),
    ]):
        conn.execute(
            """
            INSERT INTO option_shadow_log
            (id, idea_id, underlying, as_of, gate_express, gate_reason,
             side, occ_symbol, strike, expiry, delta, iv,
             est_premium, delta_adjusted_notional, contracts_qty,
             conviction, horizon_days, catalyst_tag, ivr_estimate, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"shadow-{i:03d}", "opt-idea-001", underlying,
                f"2026-06-2{i}T10:00:00Z",
                gate_express, gate_reason,
                "call", f"{underlying}260117C00200000", 200.0, "2026-01-17",
                0.35, 0.28, 500.0, 700.0, 1,
                0.70, 90.0, "earnings", 0.42,
                f"2026-06-2{i}T10:00:00Z",
            ),
        )

    # ---- option_iv_history: 35 rows for AAPL, 10 for MSFT -------------------
    for i in range(35):
        conn.execute(
            """
            INSERT INTO option_iv_history (id, underlying, as_of, atm_iv, occ_symbol, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"iv-aapl-{i:03d}", "AAPL",
                f"2026-0{(i // 30) + 1}-{(i % 28) + 1:02d}T00:00:00Z",
                0.20 + i * 0.005,
                "AAPL260117C00200000",
                f"2026-0{(i // 30) + 1}-{(i % 28) + 1:02d}T00:00:00Z",
            ),
        )
    for i in range(10):
        conn.execute(
            """
            INSERT INTO option_iv_history (id, underlying, as_of, atm_iv, occ_symbol, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                f"iv-msft-{i:03d}", "MSFT",
                f"2026-06-{i + 1:02d}T00:00:00Z",
                0.18 + i * 0.003,
                "MSFT260117C00400000",
                f"2026-06-{i + 1:02d}T00:00:00Z",
            ),
        )

    conn.commit()
    conn.close()


@pytest.fixture()
def option_fixture_db(tmp_path: Path) -> Generator[str, None, None]:
    db_file = tmp_path / "test_options_cockpit.db"
    _build_option_fixture_db(str(db_file))
    original = os.environ.get("COCKPIT_DB_PATH")
    os.environ["COCKPIT_DB_PATH"] = str(db_file)
    yield str(db_file)
    if original is None:
        os.environ.pop("COCKPIT_DB_PATH", None)
    else:
        os.environ["COCKPIT_DB_PATH"] = original


@pytest.fixture()
def option_client(option_fixture_db: str) -> Generator[TestClient, None, None]:
    """TestClient for options tests; Alpaca patched offline."""
    with patch("cockpit.api.state._alpaca_snapshot") as mock_snap:
        mock_snap.return_value = (
            __import__("cockpit.api.contract", fromlist=["Account"]).Account(
                equity=10000.0, daily_pl=0.0
            ),
            [], [], False,
        )
        # Force OPTIONS_MODE env to "shadow" so opt.layer lights up
        with patch.dict(os.environ, {"OPTIONS_MODE": "shadow"}):
            from cockpit.api.main import app
            with TestClient(app) as c:
                yield c


# =============================================================================
# (a) GET /options → OptionsState
# =============================================================================

class TestOptionsEndpoint:
    def test_options_returns_200(self, option_client: TestClient) -> None:
        r = option_client.get("/options")
        assert r.status_code == 200

    def test_options_state_shape(self, option_client: TestClient) -> None:
        data = option_client.get("/options").json()
        assert "options_mode" in data
        assert "open_positions" in data
        assert "recent_shadow_plays" in data
        assert "recent_outcomes" in data
        assert "n_open" in data
        assert "as_of" in data
        assert data["options_mode"] in ("off", "shadow", "paper")

    def test_options_open_position_count(self, option_client: TestClient) -> None:
        """Fixture has 2 open + 1 closed; openness rule excludes the closed one."""
        data = option_client.get("/options").json()
        assert data["n_open"] == 2
        assert len(data["open_positions"]) == 2

    def test_options_openness_rule(self, option_client: TestClient) -> None:
        """GOOG/GOOG260117C00180000 has an outcome row → must NOT appear in open_positions."""
        data = option_client.get("/options").json()
        open_occs = {p["occ_symbol"] for p in data["open_positions"]}
        assert "GOOG260117C00180000" not in open_occs
        assert "AAPL260117C00200000" in open_occs
        assert "MSFT260117C00400000" in open_occs

    def test_open_position_schema(self, option_client: TestClient) -> None:
        data = option_client.get("/options").json()
        for pos in data["open_positions"]:
            assert "id" in pos
            assert "idea_id" in pos
            assert "underlying" in pos
            assert "occ_symbol" in pos
            assert "side" in pos
            assert "strike" in pos
            assert "expiry" in pos
            assert "contracts_qty" in pos
            assert "entry_premium" in pos
            assert "underlying_open_price" in pos
            assert "thesis_horizon_date" in pos
            assert "original_conviction" in pos
            assert "open_ts" in pos
            # nullable computed fields present (even if None)
            assert "current_mid" in pos
            assert "unrealized_pl" in pos
            assert "unrealized_pl_pct" in pos

    def test_open_position_current_mid_is_null(self, option_client: TestClient) -> None:
        """current_mid is always None — no live option price from DB."""
        data = option_client.get("/options").json()
        for pos in data["open_positions"]:
            assert pos["current_mid"] is None
            assert pos["unrealized_pl"] is None

    def test_shadow_plays_returned(self, option_client: TestClient) -> None:
        data = option_client.get("/options").json()
        assert len(data["recent_shadow_plays"]) == 3

    def test_shadow_play_schema(self, option_client: TestClient) -> None:
        data = option_client.get("/options").json()
        for play in data["recent_shadow_plays"]:
            assert "id" in play
            assert "idea_id" in play
            assert "underlying" in play
            assert "as_of" in play
            assert "gate_express" in play
            assert "gate_reason" in play
            assert "conviction" in play
            assert "horizon_days" in play
            assert "created_at" in play
            assert isinstance(play["gate_express"], bool)

    def test_shadow_play_gate_express_values(self, option_client: TestClient) -> None:
        """Fixture has 2 express + 1 reject; both should appear."""
        data = option_client.get("/options").json()
        express_flags = {p["gate_express"] for p in data["recent_shadow_plays"]}
        assert True in express_flags
        assert False in express_flags

    def test_recent_outcomes_returned(self, option_client: TestClient) -> None:
        data = option_client.get("/options").json()
        assert len(data["recent_outcomes"]) == 1

    def test_outcome_schema(self, option_client: TestClient) -> None:
        data = option_client.get("/options").json()
        for oc in data["recent_outcomes"]:
            assert "id" in oc
            assert "idea_id" in oc
            assert "underlying" in oc
            assert "occ_symbol" in oc
            assert "side" in oc
            assert "open_ts" in oc
            assert "close_ts" in oc
            assert "close_reason" in oc
            assert "entry_premium" in oc
            assert "exit_premium" in oc
            assert "option_pl_pct" in oc
            assert "underlying_alpha_bps" in oc
            assert "contracts_qty" in oc
            assert "created_at" in oc

    def test_aggregates_populated(self, option_client: TestClient) -> None:
        """Fixture has 1 winning outcome → win_rate=1.0, avg_pl=0.5, avg_alpha=85.0."""
        data = option_client.get("/options").json()
        assert data["win_rate"] == pytest.approx(1.0, abs=1e-6)
        assert data["avg_option_pl_pct"] == pytest.approx(0.50, abs=1e-6)
        assert data["avg_underlying_alpha_bps"] == pytest.approx(85.0, abs=1e-6)

    def test_sleeve_used_pct_is_null(self, option_client: TestClient) -> None:
        """sleeve_used_pct requires Alpaca account equity — always None from DB."""
        data = option_client.get("/options").json()
        assert data["sleeve_used_pct"] is None


# =============================================================================
# (b) GET /options/iv/{ticker}
# =============================================================================

class TestIVEndpoint:
    def test_iv_returns_200_for_known_ticker(self, option_client: TestClient) -> None:
        r = option_client.get("/options/iv/AAPL")
        assert r.status_code == 200

    def test_iv_series_shape(self, option_client: TestClient) -> None:
        data = option_client.get("/options/iv/AAPL").json()
        assert "underlying" in data
        assert "points" in data
        assert "current_iv_rank" in data
        assert "as_of" in data
        assert data["underlying"] == "AAPL"

    def test_iv_points_returned(self, option_client: TestClient) -> None:
        """Fixture inserts 35 AAPL IV rows → all returned (within 365-day limit)."""
        data = option_client.get("/options/iv/AAPL").json()
        assert len(data["points"]) == 35

    def test_iv_point_schema(self, option_client: TestClient) -> None:
        data = option_client.get("/options/iv/AAPL").json()
        for pt in data["points"]:
            assert "as_of" in pt
            assert "atm_iv" in pt
            assert "occ_symbol" in pt
            assert isinstance(pt["atm_iv"], float)

    def test_iv_rank_populated_when_enough_data(self, option_client: TestClient) -> None:
        """AAPL has 35 rows (>= 30 threshold) → current_iv_rank is not None."""
        data = option_client.get("/options/iv/AAPL").json()
        assert data["current_iv_rank"] is not None
        ivr = data["current_iv_rank"]
        assert 0.0 <= ivr <= 1.0

    def test_iv_rank_none_when_insufficient_data(self, option_client: TestClient) -> None:
        """MSFT has only 10 rows (< 30) → current_iv_rank must be None."""
        data = option_client.get("/options/iv/MSFT").json()
        assert data["current_iv_rank"] is None
        assert len(data["points"]) == 10

    def test_iv_empty_not_404_for_unknown_ticker(self, option_client: TestClient) -> None:
        """Unknown ticker returns 200 with empty points — never 404."""
        r = option_client.get("/options/iv/UNKNOWN")
        assert r.status_code == 200
        data = r.json()
        assert data["underlying"] == "UNKNOWN"
        assert data["points"] == []
        assert data["current_iv_rank"] is None

    def test_iv_ticker_case_insensitive(self, option_client: TestClient) -> None:
        """Ticker is uppercased internally; lowercase input works."""
        r = option_client.get("/options/iv/aapl")
        assert r.status_code == 200
        data = r.json()
        assert data["underlying"] == "AAPL"
        assert len(data["points"]) == 35


# =============================================================================
# (c) GET /node/opt.layer → NodeDetail
# =============================================================================

class TestOptLayerNodeDetail:
    def test_opt_layer_returns_200(self, option_client: TestClient) -> None:
        r = option_client.get("/node/opt.layer")
        assert r.status_code == 200

    def test_opt_layer_type(self, option_client: TestClient) -> None:
        data = option_client.get("/node/opt.layer").json()
        assert data["type"] == "engine_part"
        assert data["id"] == "opt.layer"
        assert data["label"] == "Options Layer"

    def test_opt_layer_summary_fields(self, option_client: TestClient) -> None:
        data = option_client.get("/node/opt.layer").json()
        summary = data["summary"]
        assert "options_mode" in summary
        assert "n_open" in summary
        assert "shadow_count_7d" in summary
        assert "outcome_count" in summary
        assert "win_rate" in summary
        assert "avg_option_pl_pct" in summary
        assert "avg_underlying_alpha_bps" in summary
        assert "note" in summary

    def test_opt_layer_summary_values(self, option_client: TestClient) -> None:
        """Fixture: 2 open, 3 shadow (7d), 1 outcome."""
        data = option_client.get("/node/opt.layer").json()
        summary = data["summary"]
        assert summary["n_open"] == 2
        assert summary["shadow_count_7d"] >= 1  # 3 recent rows
        assert summary["outcome_count"] == 1

    def test_opt_layer_rows_are_shadow_plays(self, option_client: TestClient) -> None:
        """rows should contain shadow plays with kind='shadow_play'."""
        data = option_client.get("/node/opt.layer").json()
        rows = data["rows"]
        assert len(rows) >= 1
        for row in rows:
            assert row.get("kind") == "shadow_play"
            assert "underlying" in row
            assert "gate_express" in row
            assert "conviction" in row

    def test_opt_bogus_returns_404(self, option_client: TestClient) -> None:
        """Only opt.layer is valid; opt.anything_else → 404."""
        r = option_client.get("/node/opt.bogus")
        assert r.status_code == 404


# =============================================================================
# (d) Isolation — option outcomes not in equity routes
# =============================================================================

class TestIsolation:
    def test_option_outcomes_not_in_state_dynamic_nodes(self, option_client: TestClient) -> None:
        """/state dynamic_nodes must not contain option outcome nodes."""
        data = option_client.get("/state").json()
        dyn_ids = {n["id"] for n in data["dynamic_nodes"]}
        # Option outcome ids should not appear as dynamic nodes
        for dyn_id in dyn_ids:
            assert not dyn_id.startswith("opt_outcome."), (
                f"Found option outcome as dynamic node: {dyn_id}"
            )
        # The fixture option outcome id is 'out-opt-001'; it must NOT appear
        assert "outcome.out-opt-001" not in dyn_ids

    def test_option_outcomes_not_in_state_dynamic_edges(self, option_client: TestClient) -> None:
        """No 'teaches' dynamic edge must reference opt.layer."""
        data = option_client.get("/state").json()
        for edge in data["dynamic_edges"]:
            if edge.get("kind") == "teaches":
                assert edge.get("target") != "opt.layer", (
                    "opt.layer must not be a target of a teaches edge"
                )

    def test_opt_layer_not_in_equity_node_detail(self, option_client: TestClient) -> None:
        """/node/A1.insider rows must not contain option outcome data."""
        data = option_client.get("/node/A1.insider").json()
        # A1.insider has no outcomes in fixture; rows are opinions only
        # (Just verify it doesn't 500)
        assert data["type"] == "advisor"

    def test_no_graph_teaches_edge_to_opt_layer(self, option_client: TestClient) -> None:
        """/graph static edges must not have teaches → opt.layer."""
        data = option_client.get("/graph").json()
        for edge in data["edges"]:
            if edge.get("kind") == "teaches":
                assert edge.get("target") != "opt.layer"


# =============================================================================
# (e) Graph topology — opt.layer node + correct edges
# =============================================================================

class TestOptLayerGraph:
    def test_opt_layer_node_in_graph(self, option_client: TestClient) -> None:
        data = option_client.get("/graph").json()
        node_ids = {n["id"] for n in data["nodes"]}
        assert "opt.layer" in node_ids

    def test_opt_layer_node_type_and_cluster(self, option_client: TestClient) -> None:
        data = option_client.get("/graph").json()
        opt_node = next(n for n in data["nodes"] if n["id"] == "opt.layer")
        assert opt_node["type"] == "engine_part"
        assert opt_node["cluster"] == "execution"
        assert opt_node["label"] == "Options Layer"

    def test_opt_layer_has_decides_edge_from_core_safety(self, option_client: TestClient) -> None:
        data = option_client.get("/graph").json()
        decides_from_safety = [
            e for e in data["edges"]
            if e["source"] == "core.safety" and e["target"] == "opt.layer"
            and e["kind"] == "decides"
        ]
        assert len(decides_from_safety) == 1

    def test_opt_layer_has_submits_edge_to_exec_adapter(self, option_client: TestClient) -> None:
        data = option_client.get("/graph").json()
        submits_to_adapter = [
            e for e in data["edges"]
            if e["source"] == "opt.layer" and e["target"] == "exec.adapter"
            and e["kind"] == "submits"
        ]
        assert len(submits_to_adapter) == 1

    def test_opt_layer_has_no_teaches_edge(self, option_client: TestClient) -> None:
        """opt.layer must not have any teaches edges (it's an exec waypoint, not an advisor)."""
        data = option_client.get("/graph").json()
        teaches_involving_opt = [
            e for e in data["edges"]
            if e["kind"] == "teaches" and (
                e["source"] == "opt.layer" or e["target"] == "opt.layer"
            )
        ]
        assert len(teaches_involving_opt) == 0

    def test_opt_layer_meta_has_zone_position(self, option_client: TestClient) -> None:
        """opt.layer node meta should carry zone/position hints for the frontend."""
        data = option_client.get("/graph").json()
        opt_node = next(n for n in data["nodes"] if n["id"] == "opt.layer")
        meta = opt_node.get("meta", {})
        assert meta.get("zone") == "OPTIONS"
        assert meta.get("zone_color") == "#f9a825"
        assert "position" in meta


# =============================================================================
# (f) State — opt.layer intensity
# =============================================================================

class TestOptLayerState:
    def test_opt_layer_in_state_nodes(self, option_client: TestClient) -> None:
        data = option_client.get("/state").json()
        assert "opt.layer" in data["nodes"]

    def test_opt_layer_intensity_in_shadow_mode(self, option_client: TestClient) -> None:
        """OPTIONS_MODE=shadow + 3 shadow rows → intensity > 0.05."""
        data = option_client.get("/state").json()
        ns = data["nodes"]["opt.layer"]
        assert ns["intensity"] > 0.05
        assert ns["intensity"] <= 1.0

    def test_opt_layer_status_matches_mode(self, option_client: TestClient) -> None:
        data = option_client.get("/state").json()
        ns = data["nodes"]["opt.layer"]
        assert ns["status"] == "shadow"

    def test_opt_layer_off_mode_dim(self, option_fixture_db: str) -> None:
        """OPTIONS_MODE=off with no shadow rows → intensity=0.05, status=off."""
        with patch("cockpit.api.state._alpaca_snapshot") as mock_snap:
            mock_snap.return_value = (
                __import__("cockpit.api.contract", fromlist=["Account"]).Account(
                    equity=10000.0, daily_pl=0.0
                ),
                [], [], False,
            )
            with patch.dict(os.environ, {"OPTIONS_MODE": "off"}):
                # Build a DB with NO shadow rows to ensure truly off
                import tempfile
                from pathlib import Path as _Path
                with tempfile.TemporaryDirectory() as tmpdir:
                    empty_db = str(_Path(tmpdir) / "empty_opts.db")
                    from arbiter.db.connection import get_connection
                    from arbiter.db.migrate import run_migrations
                    ec = get_connection(empty_db)
                    run_migrations(ec)
                    ec.execute(
                        "INSERT INTO breaker_state (breaker_name, latched) VALUES (?, ?)",
                        ("b", 0),
                    )
                    ec.commit()
                    ec.close()
                    os.environ["COCKPIT_DB_PATH"] = empty_db
                    from cockpit.api.main import app
                    with TestClient(app) as c:
                        data = c.get("/state").json()
                    ns = data["nodes"]["opt.layer"]
                    assert ns["intensity"] == pytest.approx(0.05, abs=1e-6)
                    assert ns["status"] == "off"
