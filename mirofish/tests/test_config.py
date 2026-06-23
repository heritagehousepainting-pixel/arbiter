"""Config.from_env defaults + secret-redacting repr."""
from __future__ import annotations

from mirofish.config import Config
from mirofish.types import MEDIUM_DAYS, SHORT_DAYS


def test_defaults_when_env_unset(monkeypatch) -> None:
    # clear_env autouse fixture already unset everything.
    cfg = Config.from_env()
    assert cfg.model == "claude-sonnet-4-6"
    assert cfg.host == "127.0.0.1"
    assert cfg.port == 8900
    assert cfg.cache_ttl_seconds == 86400
    assert cfg.alpaca_data_feed == "iex"
    assert cfg.fake_llm is False
    assert cfg.short_days == SHORT_DAYS
    assert cfg.medium_days == MEDIUM_DAYS
    assert cfg.anthropic_api_key is None
    assert cfg.alpaca_secret_key is None


def test_reads_all_env_vars(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-supersecret")
    monkeypatch.setenv("EDGAR_USER_AGENT", "test agent <x@y.com>")
    monkeypatch.setenv("ALPACA_API_KEY", "PKID123")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "alpaca-supersecret")
    monkeypatch.setenv("ALPACA_DATA_FEED", "sip")
    monkeypatch.setenv("MIROFISH_MODEL", "claude-x")
    monkeypatch.setenv("MIROFISH_HOST", "::1")
    monkeypatch.setenv("MIROFISH_PORT", "9999")
    monkeypatch.setenv("MIROFISH_CACHE_TTL_SECONDS", "120")
    monkeypatch.setenv("MIROFISH_FAKE_LLM", "1")

    cfg = Config.from_env()
    assert cfg.anthropic_api_key == "sk-ant-supersecret"
    assert cfg.edgar_user_agent == "test agent <x@y.com>"
    assert cfg.alpaca_api_key == "PKID123"
    assert cfg.alpaca_secret_key == "alpaca-supersecret"
    assert cfg.alpaca_data_feed == "sip"
    assert cfg.model == "claude-x"
    assert cfg.host == "::1"
    assert cfg.port == 9999
    assert cfg.cache_ttl_seconds == 120
    assert cfg.fake_llm is True


def test_bad_port_falls_back_to_default(monkeypatch) -> None:
    monkeypatch.setenv("MIROFISH_PORT", "not-a-number")
    assert Config.from_env().port == 8900


def test_fake_llm_flag_truthiness(monkeypatch) -> None:
    for truthy in ("1", "true", "YES", "On"):
        monkeypatch.setenv("MIROFISH_FAKE_LLM", truthy)
        assert Config.from_env().fake_llm is True
    for falsy in ("0", "false", "no", ""):
        monkeypatch.setenv("MIROFISH_FAKE_LLM", falsy)
        assert Config.from_env().fake_llm is False


def test_repr_redacts_secrets() -> None:
    cfg = Config(
        anthropic_api_key="sk-ant-SECRET-A",
        alpaca_secret_key="SECRET-ALPACA",
        alpaca_api_key="PKID-not-secret",
    )
    text = repr(cfg)
    # The actual secret strings must NOT appear.
    assert "sk-ant-SECRET-A" not in text
    assert "SECRET-ALPACA" not in text
    # They are shown redacted.
    assert "***" in text
    # The non-secret api key id is allowed to appear.
    assert "PKID-not-secret" in text


def test_repr_none_secrets_not_starred() -> None:
    cfg = Config()  # all None
    text = repr(cfg)
    assert "anthropic_api_key=None" in text
    assert "alpaca_secret_key=None" in text


def test_is_loopback_host() -> None:
    assert Config(host="127.0.0.1").is_loopback_host()
    assert Config(host="::1").is_loopback_host()
    assert Config(host="localhost").is_loopback_host()
    assert not Config(host="0.0.0.0").is_loopback_host()
