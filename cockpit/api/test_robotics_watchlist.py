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
