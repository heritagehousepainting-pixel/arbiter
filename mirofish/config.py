"""Runtime configuration for the MiroFish A2 brain service.

`Config.from_env()` reads every env var the service needs (keys + tunables),
applying the frozen defaults (model `claude-sonnet-4-6`, host `127.0.0.1`,
port `8900`, cache TTL `86400`s). The `__repr__` is SECRET-REDACTING so a
config object can never leak `ANTHROPIC_API_KEY` / `ALPACA_SECRET_KEY` into a
log line or traceback.

ISOLATION: pure stdlib + mirofish.types. Never imports arbiter.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from mirofish.types import MEDIUM_DAYS, SHORT_DAYS

# Loopback hosts the service is allowed to bind to.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

_TRUTHY = frozenset({"1", "true", "yes", "on"})


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in _TRUTHY


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


@dataclass
class Config:
    """Service configuration. Secrets are redacted in `__repr__`."""

    anthropic_api_key: str | None = None
    edgar_user_agent: str | None = None
    alpaca_api_key: str | None = None
    alpaca_secret_key: str | None = None
    alpaca_data_feed: str = "iex"
    model: str = "claude-sonnet-4-6"
    host: str = "127.0.0.1"
    port: int = 8900
    cache_ttl_seconds: int = 86400
    fake_llm: bool = False
    short_days: int = SHORT_DAYS
    medium_days: int = MEDIUM_DAYS

    @classmethod
    def from_env(cls) -> "Config":
        """Build a Config from the process environment, applying defaults."""
        return cls(
            anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
            edgar_user_agent=os.environ.get("EDGAR_USER_AGENT"),
            alpaca_api_key=os.environ.get("ALPACA_API_KEY"),
            alpaca_secret_key=os.environ.get("ALPACA_SECRET_KEY"),
            alpaca_data_feed=os.environ.get("ALPACA_DATA_FEED", "iex") or "iex",
            model=os.environ.get("MIROFISH_MODEL", "claude-sonnet-4-6")
            or "claude-sonnet-4-6",
            host=os.environ.get("MIROFISH_HOST", "127.0.0.1") or "127.0.0.1",
            port=_env_int("MIROFISH_PORT", 8900),
            cache_ttl_seconds=_env_int("MIROFISH_CACHE_TTL_SECONDS", 86400),
            fake_llm=_env_bool("MIROFISH_FAKE_LLM", False),
            short_days=SHORT_DAYS,
            medium_days=MEDIUM_DAYS,
        )

    @staticmethod
    def _redact(value: str | None) -> str:
        return "***" if value else repr(value)

    def __repr__(self) -> str:
        # SECRET-REDACTING: anthropic_api_key + alpaca_secret_key never printed.
        return (
            "Config("
            f"anthropic_api_key={self._redact(self.anthropic_api_key)}, "
            f"edgar_user_agent={self.edgar_user_agent!r}, "
            f"alpaca_api_key={self.alpaca_api_key!r}, "
            f"alpaca_secret_key={self._redact(self.alpaca_secret_key)}, "
            f"alpaca_data_feed={self.alpaca_data_feed!r}, "
            f"model={self.model!r}, "
            f"host={self.host!r}, "
            f"port={self.port!r}, "
            f"cache_ttl_seconds={self.cache_ttl_seconds!r}, "
            f"fake_llm={self.fake_llm!r}, "
            f"short_days={self.short_days!r}, "
            f"medium_days={self.medium_days!r})"
        )

    def is_loopback_host(self) -> bool:
        """True iff `host` is a loopback address (safe to bind)."""
        return self.host in LOOPBACK_HOSTS
