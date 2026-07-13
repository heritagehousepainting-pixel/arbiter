"""Tests for the display-only robotics watchlist (roster module + endpoint).

Covers:
- RoboticsRosterEntry / RoboticsWatchlist DTO schema  (TestRosterEntrySchema)
- roster data hygiene + the display-only purity invariant  (TestRosterData, TestRosterPurity)
- GET /robotics-watchlist route  (TestRoboticsRoute)
"""
from __future__ import annotations

import os
import sys
import warnings
from collections.abc import Generator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

warnings.filterwarnings("ignore", category=DeprecationWarning)

# Ensure packages importable (mirror test_ticker.py)
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARBITER_ROOT = _REPO_ROOT / "arbiter"
for _p in (_REPO_ROOT, _ARBITER_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


# ---------------------------------------------------------------------------
# Shared client fixture (same pattern as test_ticker.py)
# ---------------------------------------------------------------------------
def _build_minimal_db(path: str) -> None:
    from arbiter.db.connection import get_connection
    from arbiter.db.migrate import run_migrations
    conn = get_connection(path)
    run_migrations(conn)
    conn.commit()
    conn.close()


@pytest.fixture()
def fixture_db(tmp_path: Path) -> Generator[str, None, None]:
    db_file = tmp_path / "test_robotics.db"
    _build_minimal_db(str(db_file))
    original = os.environ.get("COCKPIT_DB_PATH")
    os.environ["COCKPIT_DB_PATH"] = str(db_file)
    yield str(db_file)
    if original is None:
        os.environ.pop("COCKPIT_DB_PATH", None)
    else:
        os.environ["COCKPIT_DB_PATH"] = original


@pytest.fixture()
def client(fixture_db: str) -> Generator[TestClient, None, None]:
    with patch("cockpit.api.state._alpaca_snapshot") as mock_snap:
        mock_snap.return_value = (
            __import__("cockpit.api.contract", fromlist=["Account"]).Account(
                equity=10000.0, daily_pl=5.0
            ),
            [],
            [],
            False,
        )
        from cockpit.api.main import app
        with TestClient(app) as c:
            yield c


# ---------------------------------------------------------------------------
# Task 1 — DTO schema
# ---------------------------------------------------------------------------
class TestRosterEntrySchema:
    def test_minimal_entry(self):
        from cockpit.api.contract import RoboticsRosterEntry
        e = RoboticsRosterEntry(
            symbol="NVDA", company="Nvidia", layer="compute",
            longevity="chokepoint", priceable=True,
        )
        assert e.symbol == "NVDA"
        assert e.form_factors == []
        assert e.early_insight is False
        assert e.trigger is None

    def test_full_entry(self):
        from cockpit.api.contract import RoboticsRosterEntry
        e = RoboticsRosterEntry(
            symbol="6324.T", company="Harmonic Drive Systems", layer="components",
            form_factors=["humanoid", "industrial"], longevity="chokepoint",
            early_insight=True, trigger="Optimus mass-production ramp confirmations",
            priceable=False, region="Japan", note="strain-wave reducer near-monopoly",
        )
        assert e.priceable is False
        assert "humanoid" in e.form_factors

    def test_watchlist_wraps_entries(self):
        from cockpit.api.contract import RoboticsRosterEntry, RoboticsWatchlist
        wl = RoboticsWatchlist(
            generated="2026-07-13",
            entries=[RoboticsRosterEntry(symbol="NVDA", company="Nvidia",
                                         layer="compute", longevity="chokepoint", priceable=True)],
        )
        assert wl.generated == "2026-07-13"
        assert len(wl.entries) == 1


# ---------------------------------------------------------------------------
# Task 2 — roster data hygiene + display-only purity invariant
# ---------------------------------------------------------------------------
class TestRosterData:
    def test_roster_nonempty_and_validates(self):
        from cockpit.api.contract import RoboticsRosterEntry
        from cockpit.api.robotics_roster import robotics_roster
        rows = robotics_roster()
        assert len(rows) >= 25
        for r in rows:              # each row validates against the frozen DTO
            RoboticsRosterEntry(**r)

    def test_no_duplicate_symbols(self):
        from cockpit.api.robotics_roster import robotics_roster
        syms = [r["symbol"] for r in robotics_roster()]
        assert len(syms) == len(set(syms)), "duplicate symbols in roster"

    def test_every_layer_represented(self):
        from cockpit.api.robotics_roster import robotics_roster
        layers = {r["layer"] for r in robotics_roster()}
        assert layers == {"compute", "brain", "components", "integrator", "deployment"}

    def test_early_insight_rows_have_trigger(self):
        from cockpit.api.robotics_roster import robotics_roster
        for r in robotics_roster():
            if r.get("early_insight"):
                assert r.get("trigger"), f"{r['symbol']} early_insight without trigger"

    def test_has_both_priceable_and_reference_rows(self):
        from cockpit.api.robotics_roster import robotics_roster
        rows = robotics_roster()
        assert any(r["priceable"] for r in rows)        # US-listed charted core
        assert any(not r["priceable"] for r in rows)    # foreign/private reference rows


class TestRosterPurity:
    """The display-only invariant, refined: the roster now PROJECTS the canonical
    ``arbiter.data.robotics_universe`` (pure data), so it may import exactly that one
    module and NOTHING that reaches a trade-eligibility seam (sectors / ingest / engine /
    _DEFAULT_WATCHLIST). Being importable never makes a symbol trade-eligible."""

    _ALLOWED_ARBITER = {"arbiter.data.robotics_universe"}
    _FORBIDDEN_SUBSTRINGS = ("sectors", "ingest", "engine", "runner", "_DEFAULT_WATCHLIST")

    @staticmethod
    def _arbiter_imports(path: Path) -> list[str]:
        import ast
        names: list[str] = []
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names += [a.name for a in node.names]
            if isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module)
        return [n for n in names if n.startswith("arbiter")]

    def test_roster_imports_only_the_pure_universe(self):
        src = Path(__file__).resolve().parent / "robotics_roster.py"
        arb = self._arbiter_imports(src)
        for name in arb:
            assert name in self._ALLOWED_ARBITER, f"roster imports disallowed arbiter module: {name}"
            assert not any(s in name for s in self._FORBIDDEN_SUBSTRINGS), f"trade-seam import: {name}"

    def test_canonical_universe_is_pure_data(self):
        uni = _ARBITER_ROOT / "arbiter" / "data" / "robotics_universe.py"
        arb = self._arbiter_imports(uni)
        assert arb == [], f"canonical universe must import nothing from arbiter, got {arb}"


# ---------------------------------------------------------------------------
# Task 3 — GET /robotics-watchlist route
# ---------------------------------------------------------------------------
class TestRoboticsRoute:
    def test_returns_200_and_shape(self, client):
        r = client.get("/robotics-watchlist")
        assert r.status_code == 200
        data = r.json()
        assert data["generated"]
        assert isinstance(data["entries"], list) and len(data["entries"]) >= 25
        e = data["entries"][0]
        for field in ("symbol", "company", "layer", "longevity", "priceable",
                      "form_factors", "early_insight", "trigger", "region", "note"):
            assert field in e, f"missing {field}"

    def test_is_static_no_db(self, client):
        """Endpoint must not depend on the DB — patch connect to explode; still 200."""
        with patch("cockpit.api.main.connect", side_effect=AssertionError("DB touched")):
            r = client.get("/robotics-watchlist")
        assert r.status_code == 200
