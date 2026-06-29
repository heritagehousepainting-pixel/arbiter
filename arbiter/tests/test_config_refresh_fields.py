from arbiter.config import Config, load_config
import arbiter.config as config_module


def test_refresh_fields_default(monkeypatch):
    # Monkeypatch _load_dotenv to prevent reading ANTHROPIC_API_KEY from .env
    monkeypatch.setattr(config_module, "_load_dotenv", lambda root: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("REFRESH_MODEL", raising=False)
    cfg = load_config()
    assert cfg.anthropic_api_key == ""
    assert cfg.refresh_model == "claude-opus-4-8"
    assert cfg.a4_advisor_id == "A4.macro"
    assert 0.0 <= cfg.a4_weight_cap <= 1.0


def test_refresh_fields_from_env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-XYZ")
    monkeypatch.setenv("REFRESH_MODEL", "claude-sonnet-4-6")
    cfg = load_config()
    assert cfg.anthropic_api_key == "sk-test-XYZ"
    assert cfg.refresh_model == "claude-sonnet-4-6"


def test_anthropic_key_redacted_in_repr(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-secret-DONOTLEAK")
    cfg = load_config()
    assert "DONOTLEAK" not in repr(cfg)
