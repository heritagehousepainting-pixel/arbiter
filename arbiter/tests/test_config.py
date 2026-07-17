"""Tests for arbiter/config.py — Config loading and strict parse."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path
from unittest import mock

import pytest

from arbiter.config import Config, ConfigError, _load_dotenv, load_config


def _make_config(**overrides: object) -> Config:
    """Build a Config with dummy required fields, overridable per test."""
    base: dict[str, object] = dict(
        live_trading=False,
        executor_backend="sim",
        db_path="data/arbiter.db",
        audit_path="data/audit.jsonl",
        metrics_path="data/metrics.jsonl",
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
        edgar_user_agent="",
        kill_switch_url="",
        alert_webhook_url="",
    )
    base.update(overrides)
    return Config(**base)  # type: ignore[arg-type]


class TestConfigDefaults:
    def test_live_trading_defaults_false(self, tmp_path: Path) -> None:
        """INTERFACES.md §11 convention 4: LIVE_TRADING defaults False."""
        # Write a minimal valid TOML
        toml = tmp_path / "arbiter.toml"
        toml.write_text("[core]\n# no live_trading key\n", encoding="utf-8")

        cfg = load_config(config_path=toml)
        assert cfg.live_trading is False

    def test_returns_config_dataclass(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("", encoding="utf-8")

        cfg = load_config(config_path=toml)
        assert isinstance(cfg, Config)

    def test_config_is_frozen(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("", encoding="utf-8")

        cfg = load_config(config_path=toml)
        with pytest.raises((AttributeError, TypeError)):
            cfg.live_trading = True  # type: ignore[misc]

    def test_sizing_defaults(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # This asserts the CODE defaults, so it must not inherit sizing overrides
        # that a developer's .env may set (e.g. the $10k-account guardrails).
        # load_config() calls _load_dotenv() which re-populates os.environ from the
        # real .env via setdefault, so we both no-op the dotenv load AND clear any
        # already-leaked sizing vars.
        monkeypatch.setattr("arbiter.config._load_dotenv", lambda *a, **k: None)
        for _var in (
            "ARBITER_MAX_POSITION_PCT",
            "ARBITER_MAX_SECTOR_PCT",
            "ARBITER_MAX_GROSS_PCT",
            "ARBITER_MAX_OPEN_POSITIONS",
            "ARBITER_ADV_CAP_PCT",
        ):
            monkeypatch.delenv(_var, raising=False)

        toml = tmp_path / "arbiter.toml"
        toml.write_text("", encoding="utf-8")

        cfg = load_config(config_path=toml)
        assert cfg.max_position_pct == pytest.approx(0.05)
        assert cfg.max_sector_pct == pytest.approx(0.20)
        assert cfg.max_gross_pct == pytest.approx(0.80)
        assert cfg.max_open_positions == 20
        assert cfg.adv_cap_pct == pytest.approx(0.02)


class TestStrictParse:
    def test_unknown_top_level_section_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("[mystery_section]\nfoo = 1\n", encoding="utf-8")

        with pytest.raises(ConfigError, match="mystery_section"):
            load_config(config_path=toml)

    def test_unknown_key_in_known_section_raises(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("[core]\nunknown_key = true\n", encoding="utf-8")

        with pytest.raises(ConfigError, match="unknown_key"):
            load_config(config_path=toml)

    def test_known_keys_accepted(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text(
            "[core]\nlive_trading = false\n[sizing]\nmax_position_pct = 0.05\n",
            encoding="utf-8",
        )
        cfg = load_config(config_path=toml)
        assert cfg.live_trading is False
        assert cfg.max_position_pct == pytest.approx(0.05)


class TestEnvOverride:
    def test_live_trading_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("[core]\nlive_trading = false\n", encoding="utf-8")

        monkeypatch.setenv("LIVE_TRADING", "true")
        cfg = load_config(config_path=toml)
        assert cfg.live_trading is True

    def test_db_path_env_override(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("", encoding="utf-8")

        monkeypatch.setenv("ARBITER_DB_PATH", "/tmp/custom.db")
        cfg = load_config(config_path=toml)
        assert cfg.db_path == "/tmp/custom.db"


class TestDotenvDiscovery:
    """Verify that config.py loads .env from parents[1] of config.py (project root).

    The critical bug was: parents[2] pointed one level too high (to poly_bot/,
    not arbiter/).  These tests use a tmp directory to simulate the package
    layout and confirm _load_dotenv reads from the correct location without
    touching real secrets.
    """

    def test_dotenv_loaded_from_project_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """_load_dotenv(root) reads KEY=VALUE from root/.env."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_ARBITER_SENTINEL=hello_from_dotenv\n", encoding="utf-8")

        # Ensure the key is absent before the call.
        monkeypatch.delenv("TEST_ARBITER_SENTINEL", raising=False)

        _load_dotenv(tmp_path)

        assert os.environ.get("TEST_ARBITER_SENTINEL") == "hello_from_dotenv"
        # Clean up so we don't leak across tests.
        monkeypatch.delenv("TEST_ARBITER_SENTINEL", raising=False)

    def test_dotenv_real_env_wins_over_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A variable already in the environment is NOT overwritten by .env."""
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_ARBITER_SENTINEL=from_file\n", encoding="utf-8")

        monkeypatch.setenv("TEST_ARBITER_SENTINEL", "from_env")
        _load_dotenv(tmp_path)

        assert os.environ["TEST_ARBITER_SENTINEL"] == "from_env"

    def test_dotenv_missing_file_is_noop(self, tmp_path: Path) -> None:
        """_load_dotenv must not raise when .env does not exist."""
        _load_dotenv(tmp_path / "nonexistent")  # no .env here

    def test_config_py_root_is_project_root(self) -> None:
        """parents[1] of config.py must be the project root (contains pyproject.toml).

        This directly guards against the parents[2] regression.  If this test
        breaks, config.py is pointing at the wrong ancestor directory.
        """
        import arbiter.config as config_mod

        config_file = Path(config_mod.__file__).resolve()
        derived_root = config_file.parents[1]
        assert (derived_root / "pyproject.toml").exists(), (
            f"parents[1] of config.py is {derived_root!r} which does not contain "
            "pyproject.toml — this means parents[1] is NOT the project root. "
            "Check whether the parents[2] regression was re-introduced."
        )


class TestPaperUrlValidation:
    """[A3, P1] paper base url host must be the real paper host or a loopback."""

    @pytest.fixture(autouse=True)
    def _isolate_paper_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Don't let the developer's real .env override the TOML under test.

        load_config() calls _load_dotenv() which re-populates os.environ from the
        real .env via setdefault (env wins over TOML). No-op the dotenv load AND
        clear any already-leaked ALPACA_PAPER_BASE_URL so the TOML value is used.
        """
        monkeypatch.setattr("arbiter.config._load_dotenv", lambda *a, **k: None)
        monkeypatch.delenv("ALPACA_PAPER_BASE_URL", raising=False)

    def test_real_paper_host_passes(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text(
            "[alpaca]\npaper_base_url = "
            '"https://paper-api.alpaca.markets"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_path=toml)
        assert cfg.alpaca_paper_base_url == "https://paper-api.alpaca.markets"

    def test_localhost_passes(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text(
            '[alpaca]\npaper_base_url = "http://localhost:8080"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_path=toml)
        assert cfg.alpaca_paper_base_url == "http://localhost:8080"

    def test_loopback_ip_passes(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text(
            '[alpaca]\npaper_base_url = "http://127.0.0.1:9000"\n',
            encoding="utf-8",
        )
        cfg = load_config(config_path=toml)
        assert cfg.alpaca_paper_base_url == "http://127.0.0.1:9000"

    def test_live_host_rejected_fail_closed(self, tmp_path: Path) -> None:
        """A .env editing the paper url to the LIVE endpoint must fail-closed."""
        toml = tmp_path / "arbiter.toml"
        toml.write_text(
            '[alpaca]\npaper_base_url = "https://api.alpaca.markets"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError, match="paper"):
            load_config(config_path=toml)

    def test_arbitrary_host_rejected(self, tmp_path: Path) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text(
            '[alpaca]\npaper_base_url = "https://evil.example.com"\n',
            encoding="utf-8",
        )
        with pytest.raises(ConfigError):
            load_config(config_path=toml)

    def test_env_override_to_live_host_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        toml = tmp_path / "arbiter.toml"
        toml.write_text("", encoding="utf-8")
        monkeypatch.setattr("arbiter.config._load_dotenv", lambda *a, **k: None)
        monkeypatch.setenv("ALPACA_PAPER_BASE_URL", "https://api.alpaca.markets")
        with pytest.raises(ConfigError):
            load_config(config_path=toml)


class TestRedactingRepr:
    """[J1, P1] repr/str must mask secrets so log.info(config) can't leak them."""

    def test_repr_masks_api_and_secret_keys(self) -> None:
        cfg = _make_config(
            alpaca_api_key="PKDUMMYAPIKEY1234567",
            alpaca_secret_key="dummysecretkeyABCDEFG987654321",
        )
        text = repr(cfg)
        assert "PKDUMMYAPIKEY1234567" not in text
        assert "dummysecretkeyABCDEFG987654321" not in text
        assert "REDACTED" in text

    def test_str_masks_secrets(self) -> None:
        cfg = _make_config(
            alpaca_api_key="PKDUMMYAPIKEY1234567",
            alpaca_secret_key="dummysecretkeyABCDEFG987654321",
        )
        assert "PKDUMMYAPIKEY1234567" not in str(cfg)
        assert "dummysecretkeyABCDEFG987654321" not in str(cfg)

    def test_repr_masks_webhook_and_kill_switch_urls(self) -> None:
        cfg = _make_config(
            kill_switch_url="https://hooks.example.com/SECRETKILLTOKEN",
            alert_webhook_url="https://hooks.example.com/SECRETALERTTOKEN",
        )
        text = repr(cfg)
        assert "SECRETKILLTOKEN" not in text
        assert "SECRETALERTTOKEN" not in text

    def test_repr_keeps_nonsecret_fields_visible(self) -> None:
        cfg = _make_config(executor_backend="alpaca_paper", max_open_positions=20)
        text = repr(cfg)
        assert "alpaca_paper" in text
        assert "20" in text

    def test_empty_secrets_do_not_crash_repr(self) -> None:
        cfg = _make_config()
        # Must not raise; empty secrets are fine.
        repr(cfg)
        str(cfg)


class TestTrustParoleFraction:
    """Unfreeze Stage 2 config knob."""

    def test_default(self):
        cfg = load_config()
        assert cfg.trust_parole_fraction == 0.5

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("ARBITER_TRUST_PAROLE_FRACTION", "0.3")
        cfg = load_config()
        assert cfg.trust_parole_fraction == 0.3
