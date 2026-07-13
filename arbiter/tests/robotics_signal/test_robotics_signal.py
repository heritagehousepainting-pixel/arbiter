"""Hermetic tests for the robotics-signal scanner core (part 1).

Uses arbiter.refresh.llm.FakeLLM — no real Anthropic call, no real webhook.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

from arbiter.refresh.llm import FakeLLM
from arbiter.robotics_signal.digest import build_digest
from arbiter.robotics_signal.orchestrator import run_robotics_scan
from arbiter.robotics_signal.scan import parse_robotics_json, scan_robotics
from arbiter.robotics_signal.types import RoboticsReport, RoboticsScanResult

AS_OF = datetime(2026, 7, 13, 8, 0)

CANNED = """Here are the findings:
```json
{"developments": [
  {"headline": "Bosch places production order with Neura Robotics",
   "summary": "Bosch moved from co-development to production orders.",
   "category": "integrator", "symbols": ["NEURA"],
   "trigger_hit": true, "trigger_name": "NEURA",
   "sources": ["https://example.com/neura"]},
  {"headline": "Nvidia ships Jetson Thor",
   "summary": "General availability of Thor.", "category": "compute",
   "symbols": ["NVDA"], "trigger_hit": false, "trigger_name": null,
   "sources": ["https://example.com/nvda"]}
]}
```
That's all."""


def _config(key: str = "test") -> SimpleNamespace:
    return SimpleNamespace(anthropic_api_key=key, robotics_model="",
                           refresh_model="claude-opus-4-8")


def _engine(alerting=None, config=None) -> SimpleNamespace:
    return SimpleNamespace(
        config=config or _config(),
        clock=SimpleNamespace(now=lambda: AS_OF),
        alerting=alerting,
    )


# ---------------------------------------------------------------------------
# parse
# ---------------------------------------------------------------------------
class TestParse:
    def test_extracts_developments(self):
        devs = parse_robotics_json(CANNED, {"NEURA", "NVDA"})
        assert len(devs) == 2
        assert devs[0].trigger_hit and devs[0].trigger_name == "NEURA"
        assert devs[1].trigger_hit is False and devs[1].trigger_name is None

    def test_unknown_trigger_symbol_is_demoted(self):
        text = ('```json\n{"developments": [{"headline": "x", "summary": "y", '
                '"category": "brain", "trigger_hit": true, "trigger_name": "FAKE"}]}\n```')
        devs = parse_robotics_json(text, {"NEURA"})
        assert devs[0].trigger_hit is False and devs[0].trigger_name is None

    def test_bad_category_falls_back_to_other(self):
        text = ('```json\n{"developments": [{"headline": "x", "summary": "y", '
                '"category": "nonsense"}]}\n```')
        assert parse_robotics_json(text, set())[0].category == "other"


# ---------------------------------------------------------------------------
# scan (fail-closed)
# ---------------------------------------------------------------------------
class TestScan:
    def test_happy_path_with_fakellm(self):
        res = scan_robotics(AS_OF, _config(), llm=FakeLLM(CANNED))
        assert res.available is True
        assert len(res.developments) == 2
        assert len(res.trigger_hits) == 1
        assert res.trigger_hits[0].trigger_name == "NEURA"

    def test_no_key_no_llm_skips(self):
        res = scan_robotics(AS_OF, _config(key=""), llm=None)
        assert res.available is False and "ANTHROPIC_API_KEY" in res.note

    def test_bad_json_fails_closed(self):
        res = scan_robotics(AS_OF, _config(), llm=FakeLLM("no json at all"))
        assert res.available is False and res.developments == []

    def test_llm_raises_fails_closed(self):
        class Boom:
            def create(self, **_):
                raise RuntimeError("api down")
        res = scan_robotics(AS_OF, _config(), llm=Boom())
        assert res.available is False


# ---------------------------------------------------------------------------
# digest
# ---------------------------------------------------------------------------
class TestDigest:
    def test_renders_hits_and_developments(self):
        scan = scan_robotics(AS_OF, _config(), llm=FakeLLM(CANNED))
        text = build_digest(RoboticsReport(as_of=AS_OF, scan=scan))
        assert "ROBOTICS SIGNAL" in text
        assert "TRIGGER HITS (1)" in text
        assert "NEURA" in text
        assert "DEVELOPMENTS (1)" in text

    def test_unavailable_scan_notes_it(self):
        scan = RoboticsScanResult(available=False, note="skipped (no ANTHROPIC_API_KEY)")
        text = build_digest(RoboticsReport(as_of=AS_OF, scan=scan))
        assert "skipped" in text


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------
class TestOrchestrator:
    def test_scans_and_pushes(self):
        alerting = MagicMock()
        report = run_robotics_scan(_engine(alerting=alerting), llm=FakeLLM(CANNED))
        assert report.scan.available is True
        assert len(report.scan.trigger_hits) == 1
        alerting.notify.assert_called_once()
        # phone push carries the run timestamp
        assert alerting.notify.call_args.kwargs["as_of"] == AS_OF

    def test_fail_closed_still_pushes_and_never_raises(self):
        class Boom:
            def create(self, **_):
                raise RuntimeError("api down")
        alerting = MagicMock()
        report = run_robotics_scan(_engine(alerting=alerting), llm=Boom())
        assert report.scan.available is False
        alerting.notify.assert_called_once()  # still notifies, with the unavailable headline
