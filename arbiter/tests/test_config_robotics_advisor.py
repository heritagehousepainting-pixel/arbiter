"""Config tests for the probationary A5.robotics advisor knobs (#3d).

The advisor is DORMANT by default (``robotics_advisor_enabled=False``) — a
kill-switch that must default OFF until the creator explicitly flips it. Mirrors
the A4.macro knobs (``a4_min_stance`` / ``a4_min_confidence`` / ``a4_weight_cap`` /
``a4_advisor_id``).
"""
from arbiter.config import load_config
import arbiter.config as config_module


def _isolate_env(monkeypatch):
    """Prevent .env + real env from bleeding into the default-value assertions."""
    monkeypatch.setattr(config_module, "_load_dotenv", lambda root: None)
    for var in ("ROBOTICS_ADVISOR_ENABLED", "A5_MIN_STANCE", "A5_MIN_CONFIDENCE",
                "A5_WEIGHT_CAP", "A5_ADVISOR_ID"):
        monkeypatch.delenv(var, raising=False)


def test_robotics_advisor_disabled_by_default(monkeypatch):
    _isolate_env(monkeypatch)
    cfg = load_config()
    assert cfg.robotics_advisor_enabled is False


def test_a5_knob_defaults(monkeypatch):
    _isolate_env(monkeypatch)
    cfg = load_config()
    assert cfg.a5_advisor_id == "A5.robotics"
    assert cfg.a5_min_stance == 0.25
    assert cfg.a5_min_confidence == 0.0
    # A small, probationary cap — never as loud as a graduated advisor.
    assert 0.0 < cfg.a5_weight_cap <= 0.5


def test_robotics_advisor_enabled_from_env(monkeypatch):
    monkeypatch.setenv("ROBOTICS_ADVISOR_ENABLED", "1")
    cfg = load_config()
    assert cfg.robotics_advisor_enabled is True


def test_a5_knobs_from_env(monkeypatch):
    monkeypatch.setenv("A5_MIN_STANCE", "0.4")
    monkeypatch.setenv("A5_MIN_CONFIDENCE", "0.2")
    monkeypatch.setenv("A5_WEIGHT_CAP", "0.1")
    monkeypatch.setenv("A5_ADVISOR_ID", "A5.robotics.test")
    cfg = load_config()
    assert cfg.a5_min_stance == 0.4
    assert cfg.a5_min_confidence == 0.2
    assert cfg.a5_weight_cap == 0.1
    assert cfg.a5_advisor_id == "A5.robotics.test"
