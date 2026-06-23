"""Pytest configuration and shared fixtures for arbiter tests."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Ensure the arbiter package is importable when running pytest from the
# project root (arbiter/).  The flat layout means the package lives at
# arbiter/arbiter/, so we add arbiter/ to sys.path.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


@pytest.fixture()
def tmp_db(tmp_path: Path) -> str:
    """Return a path to a temporary SQLite file (deleted after test)."""
    return str(tmp_path / "test_arbiter.db")


@pytest.fixture()
def memory_db() -> str:
    """Return ':memory:' for a pure in-memory SQLite connection."""
    return ":memory:"
