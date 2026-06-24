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

# ---------------------------------------------------------------------------
# Test hermeticity — NEVER let the suite reach live infrastructure.
# ---------------------------------------------------------------------------
# ``load_config()`` reads the real ``.env`` (where ALERT_WEBHOOK_URL and
# KILL_SWITCH_URL point at the user's live ntfy topic / Cloudflare worker). A
# test that builds a real ``Alerting``/engine from that config would POST real
# *critical* alerts to the user's PHONE (this happened: a full ``pytest`` run
# blasted the ntfy app). ``_load_dotenv`` uses ``os.environ.setdefault``, so a
# value already present in the environment wins over the .env file — force
# these empty at import time (before any test loads config) so the webhook URL
# resolves to "" and ``Alerting._post_webhook`` early-returns.
for _hermetic_key in ("ALERT_WEBHOOK_URL", "KILL_SWITCH_URL"):
    os.environ[_hermetic_key] = ""


# Real infra hosts that must never receive a packet from the test suite.
_BLOCKED_ALERT_HOSTS = ("ntfy.sh", "workers.dev")


@pytest.fixture(autouse=True)
def _block_real_alert_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    """Belt-and-suspenders: block the alerting webhook POST to REAL infra hosts.

    The env scrub above is the primary guard (empty URL → no POST). This wraps
    ``Alerting._post_webhook`` (NOT the shared ``httpx`` module — patching that
    globally would also clobber unrelated callers like the MiroFish adapter) and
    drops any POST whose URL targets the live ntfy topic / Cloudflare worker, so
    even a test that hardcodes the real URL can never page the phone. Test fakes
    (e.g. ``http://infra.example/webhook`` in ``test_alerting.py``) are NOT
    blocked, so their own ``httpx.post`` mocks still receive the call.
    """
    from arbiter.safety import alerting as _alerting

    _orig_post_webhook = _alerting.Alerting._post_webhook

    def _guarded(self, *, tier, message, ctx, ts):  # type: ignore[no-untyped-def]
        url = self.config.alert_webhook_url or ""
        if any(host in url for host in _BLOCKED_ALERT_HOSTS):
            return None  # block real-infra egress; auto-pause already handled
        return _orig_post_webhook(self, tier=tier, message=message, ctx=ctx, ts=ts)

    monkeypatch.setattr(_alerting.Alerting, "_post_webhook", _guarded, raising=True)


@pytest.fixture()
def tmp_db(tmp_path: Path) -> str:
    """Return a path to a temporary SQLite file (deleted after test)."""
    return str(tmp_path / "test_arbiter.db")


@pytest.fixture()
def memory_db() -> str:
    """Return ':memory:' for a pure in-memory SQLite connection."""
    return ":memory:"
