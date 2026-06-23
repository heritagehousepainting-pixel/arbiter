"""Strict-parse configuration for Arbiter.

Loads ``config/arbiter.toml`` then applies env-var overrides.
Unknown TOML keys raise ``ConfigError`` (fail-closed).
``LIVE_TRADING`` defaults False (INTERFACES.md §11 convention 4).

Downstream agents: the frozen ``Config`` dataclass fields are:

    live_trading: bool
    executor_backend: str  # "sim" (default) | "alpaca_paper"
    db_path: str
    audit_path: str
    metrics_path: str
    max_position_pct: float
    max_sector_pct: float
    max_gross_pct: float
    max_open_positions: int
    adv_cap_pct: float
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper_base_url: str
    alpaca_data_base_url: str
    alpaca_timeout: float
    edgar_user_agent: str
    kill_switch_url: str
    alert_webhook_url: str
    fast_interval_s: float
    full_cycle_times_et: str
    daemon_heartbeat_path: str
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlparse


class ConfigError(Exception):
    """Raised when the config file contains unknown keys or invalid values."""


# Allowed values for the executor_backend field (fail-closed on typos).
_VALID_EXECUTOR_BACKENDS = {"sim", "alpaca_paper"}

# [A3, P1] paper base url host allow-list (fail-closed).  Permit ONLY the real
# Alpaca paper host or a loopback host (tests/mocks).  Anything else — notably a
# live-money trading host — is rejected so a stray .env edit can't silently
# route "paper" orders to a live endpoint. (No live host string is named here on
# purpose: the paper-only tripwire test forbids that literal anywhere.)
_PAPER_HOST = "paper-api.alpaca.markets"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# [J1, P1] Config fields whose values must be masked in repr/str.  Exact field
# names plus a suffix rule for any *_webhook_url.
_SECRET_FIELDS = {"alpaca_api_key", "alpaca_secret_key", "kill_switch_url"}
_REDACTED = "***REDACTED***"


def _is_secret_field(name: str) -> bool:
    return name in _SECRET_FIELDS or name.endswith("_webhook_url")


def _mask_secret(value: object) -> str:
    """Mask a secret value: empty stays empty; otherwise show last 4 chars."""
    text = str(value)
    if not text:
        return ""
    tail = text[-4:] if len(text) >= 4 else ""
    return f"{_REDACTED}{tail}"


def _validate_paper_base_url(url: str) -> None:
    """[A3, P1] Raise ConfigError unless ``url`` host is the paper host or loopback."""
    host = (urlparse(url).hostname or "").lower()
    if host == _PAPER_HOST or host in _LOOPBACK_HOSTS:
        return
    raise ConfigError(
        f"alpaca_paper_base_url host {host!r} is not the Alpaca paper host "
        f"({_PAPER_HOST!r}) or a loopback host; refusing to route 'paper' "
        f"orders to a non-paper endpoint (fail-closed)."
    )


# ---------------------------------------------------------------------------
# Known keys per section — strict parse rejects anything outside these sets.
# ---------------------------------------------------------------------------
_KNOWN_KEYS: dict[str, set[str]] = {
    "core": {"live_trading", "executor_backend", "trust_equal_floor"},
    "sizing": {
        "max_position_pct",
        "max_sector_pct",
        "max_gross_pct",
        "max_open_positions",
        "adv_cap_pct",
    },
    "storage": {"db_path", "audit_path", "metrics_path"},
    "alpaca": {
        "api_key",
        "secret_key",
        "paper_base_url",
        "data_base_url",
        "timeout",
    },
    "edgar": {"user_agent"},
    "finnhub": {"api_key", "min_stance", "min_confidence"},
    "alerting": {"kill_switch_url", "alert_webhook_url"},
    "daemon": {"fast_interval_s", "full_cycle_times_et", "heartbeat_path"},
}


def _validate_toml(data: dict) -> None:
    """Raise ConfigError if ``data`` contains any key not in _KNOWN_KEYS."""
    unknown_sections = set(data.keys()) - set(_KNOWN_KEYS.keys())
    if unknown_sections:
        raise ConfigError(f"Unknown top-level TOML sections: {sorted(unknown_sections)}")
    for section, keys in data.items():
        if not isinstance(keys, dict):
            continue
        unknown = set(keys.keys()) - _KNOWN_KEYS.get(section, set())
        if unknown:
            raise ConfigError(
                f"Unknown keys in [{section}]: {sorted(unknown)}"
            )


@dataclass(frozen=True)
class Config:
    """Frozen runtime configuration.  All fields are typed; no mutation after load."""

    # Core
    live_trading: bool
    executor_backend: str  # "sim" (default) | "alpaca_paper" — selects the broker

    # Storage
    db_path: str
    audit_path: str
    metrics_path: str

    # Sizing caps (INTERFACES.md §9)
    max_position_pct: float
    max_sector_pct: float
    max_gross_pct: float
    max_open_positions: int
    adv_cap_pct: float

    # Alpaca
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_paper_base_url: str
    alpaca_data_base_url: str
    alpaca_timeout: float

    # EDGAR
    edgar_user_agent: str

    # Alerting / kill switch
    kill_switch_url: str
    alert_webhook_url: str

    # Daemon runtime (sub-project #3 — INTERFACES §10b.5).  Defaulted so existing
    # direct ``Config(...)`` constructions (tests) need no change.
    fast_interval_s: float = 180.0
    full_cycle_times_et: str = "09:45,15:30"
    daemon_heartbeat_path: str = "data/arbiter-daemon.heartbeat"

    # Learning loop (sub-project #4, D3).  Probationary floor weight a cold/shadow
    # advisor trades at so the bucket never deadlocks.  A FRACTION (not 1.0) so an
    # unproven advisor can't dominate a graduated proven one.  Default 0.25 — kept
    # strictly BELOW the ledger's graduated weight CEILING (0.50) so a cold advisor
    # can never reach parity with a fully-graduated max-trust advisor.  Defaulted so
    # existing direct ``Config(...)`` constructions need no change.
    trust_equal_floor: float = 0.25

    # A3 News advisor (Finnhub) — free API key (non-commercial personal use).
    # Register at https://finnhub.io (instant, no card).  Empty = A3 inert.
    # Env var: FINNHUB_API_KEY.
    finnhub_api_key: str = ""

    # A3 strength gate — a corroborated ticker is only emitted as an Opinion
    # when abs(stance_score) >= a3_min_stance.  Default 0.25 cuts the ~0.05–0.18
    # mild scores from the live distribution while keeping the strong signals
    # (NVDA 0.51, UNH 0.51, TSLA 0.30, META 0.28 in a 10-ticker live run).
    # Env var: A3_MIN_STANCE.
    a3_min_stance: float = 0.25

    # A3 confidence floor — optional secondary gate.  Default 0.0 (disabled):
    # the stance gate already cuts noisy low-signal tickers; the confidence
    # formula (corroboration + source_tier + recency) is not a reliable proxy
    # for signal strength.  Raise this only if you want to further require
    # at least a minimum publisher quality / corroboration level.
    # Env var: A3_MIN_CONFIDENCE.
    a3_min_confidence: float = 0.0

    # A1.fund (Form 13F) advisor — quarterly fund-manager holdings signals.
    # Env var: FORM13F_MIN_POSITION_USD
    form13f_min_position_usd: float = 10_000_000.0
    # Env var: FORM13F_MIN_BOOK_FRACTION
    form13f_min_book_fraction: float = 0.005
    # Env var: FORM13F_MIN_DELTA_FRACTION
    form13f_min_delta_fraction: float = 0.25
    # Env var: FORM13F_FIRST_FILING_TOP_K
    form13f_first_filing_top_k: int = 5
    # Env var: FORM13F_MAX_CONVICTION
    form13f_max_conviction: float = 0.7
    # Comma-separated 10-digit CIKs; None when unset.
    # Env var: FORM13F_MANAGER_CIKS
    form13f_manager_ciks: tuple[str, ...] | None = None

    def __repr__(self) -> str:
        """[J1, P1] Redacting repr — masks secrets so ``log.info(config)`` can't leak.

        API/secret keys and any ``*_webhook_url``/``kill_switch_url`` are shown
        as ``***REDACTED***<last4>`` (empty values stay empty); all other fields
        render normally.
        """
        parts = []
        for f in fields(self):
            value = getattr(self, f.name)
            if _is_secret_field(f.name):
                parts.append(f"{f.name}={_mask_secret(value)!r}")
            else:
                parts.append(f"{f.name}={value!r}")
        return f"Config({', '.join(parts)})"

    __str__ = __repr__


# ---------------------------------------------------------------------------
# Env-var helper functions (pattern from stockbot/src/config.py)
# ---------------------------------------------------------------------------

def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw in (None, ""):
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name: str, default: str) -> str:
    raw = os.getenv(name)
    return default if raw in (None, "") else raw.strip()


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return default if raw in (None, "") else float(raw)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return default if raw in (None, "") else int(raw)


def _load_dotenv(root: Path) -> None:
    """Load ``root/.env`` into ``os.environ`` if present (dependency-free).

    Uses setdefault semantics — a variable already exported in the real
    environment ALWAYS wins over the ``.env`` file. Lines that are blank,
    comments (``#``), or lack ``=`` are skipped. Surrounding quotes on values
    are stripped. Safe to call repeatedly.
    """
    env_path = root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------

def _parse_form13f_manager_ciks() -> tuple[str, ...] | None:
    """Parse FORM13F_MANAGER_CIKS env var into a tuple of stripped 10-digit strings.

    Returns None when the variable is unset or empty.
    """
    raw = os.getenv("FORM13F_MANAGER_CIKS")
    if not raw or not raw.strip():
        return None
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def load_config(config_path: Path | None = None) -> Config:
    """Load and return a frozen ``Config``.

    Resolution order (later wins):
    1. Defaults baked into this function
    2. ``config/arbiter.toml`` (or ``config_path`` override)
    3. Environment variables

    Raises ``ConfigError`` on unknown TOML keys.
    """
    root = Path(__file__).resolve().parents[1]
    # Load .env (real environment variables still take precedence).
    _load_dotenv(root)

    if config_path is None:
        config_path = root / "config" / "arbiter.toml"

    data: dict = {}
    if config_path.exists():
        with open(config_path, "rb") as fh:
            data = tomllib.load(fh)
        _validate_toml(data)

    core = data.get("core", {})
    sizing = data.get("sizing", {})
    storage = data.get("storage", {})
    alpaca = data.get("alpaca", {})
    edgar = data.get("edgar", {})
    finnhub = data.get("finnhub", {})
    alerting = data.get("alerting", {})
    daemon = data.get("daemon", {})

    executor_backend = _env_str(
        "EXECUTOR_BACKEND", str(core.get("executor_backend", "sim"))
    )
    if executor_backend not in _VALID_EXECUTOR_BACKENDS:
        raise ConfigError(
            f"Invalid executor_backend {executor_backend!r}; "
            f"must be one of {sorted(_VALID_EXECUTOR_BACKENDS)}"
        )

    alpaca_paper_base_url = _env_str(
        "ALPACA_PAPER_BASE_URL",
        str(alpaca.get("paper_base_url", "https://paper-api.alpaca.markets")),
    )
    # [A3, P1] Fail-closed: a non-paper / non-loopback host is rejected here so a
    # stray .env edit can never silently route "paper" orders to a live endpoint.
    _validate_paper_base_url(alpaca_paper_base_url)

    return Config(
        live_trading=_env_bool("LIVE_TRADING", bool(core.get("live_trading", False))),
        executor_backend=executor_backend,
        db_path=_env_str("ARBITER_DB_PATH", str(storage.get("db_path", "data/arbiter.db"))),
        audit_path=_env_str("ARBITER_AUDIT_PATH", str(storage.get("audit_path", "data/audit.jsonl"))),
        metrics_path=_env_str("ARBITER_METRICS_PATH", str(storage.get("metrics_path", "data/metrics.jsonl"))),
        max_position_pct=_env_float("ARBITER_MAX_POSITION_PCT", float(sizing.get("max_position_pct", 0.05))),
        max_sector_pct=_env_float("ARBITER_MAX_SECTOR_PCT", float(sizing.get("max_sector_pct", 0.20))),
        max_gross_pct=_env_float("ARBITER_MAX_GROSS_PCT", float(sizing.get("max_gross_pct", 0.80))),
        max_open_positions=_env_int("ARBITER_MAX_OPEN_POSITIONS", int(sizing.get("max_open_positions", 20))),
        adv_cap_pct=_env_float("ARBITER_ADV_CAP_PCT", float(sizing.get("adv_cap_pct", 0.02))),
        alpaca_api_key=_env_str("ALPACA_API_KEY", str(alpaca.get("api_key", ""))),
        alpaca_secret_key=_env_str("ALPACA_SECRET_KEY", str(alpaca.get("secret_key", ""))),
        alpaca_paper_base_url=alpaca_paper_base_url,
        alpaca_data_base_url=_env_str(
            "ALPACA_DATA_BASE_URL",
            str(alpaca.get("data_base_url", "https://data.alpaca.markets")),
        ),
        alpaca_timeout=_env_float("ALPACA_TIMEOUT", float(alpaca.get("timeout", 20.0))),
        edgar_user_agent=_env_str("EDGAR_USER_AGENT", str(edgar.get("user_agent", ""))),
        finnhub_api_key=_env_str("FINNHUB_API_KEY", str(finnhub.get("api_key", ""))),
        a3_min_stance=_env_float("A3_MIN_STANCE", float(finnhub.get("min_stance", 0.25))),
        a3_min_confidence=_env_float("A3_MIN_CONFIDENCE", float(finnhub.get("min_confidence", 0.0))),
        kill_switch_url=_env_str("KILL_SWITCH_URL", str(alerting.get("kill_switch_url", ""))),
        alert_webhook_url=_env_str("ALERT_WEBHOOK_URL", str(alerting.get("alert_webhook_url", ""))),
        fast_interval_s=_env_float("ARBITER_FAST_INTERVAL_S", float(daemon.get("fast_interval_s", 180.0))),
        full_cycle_times_et=_env_str("ARBITER_FULL_CYCLE_TIMES_ET", str(daemon.get("full_cycle_times_et", "09:45,15:30"))),
        daemon_heartbeat_path=_env_str(
            "ARBITER_DAEMON_HEARTBEAT_PATH",
            str(daemon.get("heartbeat_path", "data/arbiter-daemon.heartbeat")),
        ),
        trust_equal_floor=_env_float(
            "ARBITER_TRUST_EQUAL_FLOOR", float(core.get("trust_equal_floor", 0.25))
        ),
        form13f_min_position_usd=_env_float("FORM13F_MIN_POSITION_USD", 10_000_000.0),
        form13f_min_book_fraction=_env_float("FORM13F_MIN_BOOK_FRACTION", 0.005),
        form13f_min_delta_fraction=_env_float("FORM13F_MIN_DELTA_FRACTION", 0.25),
        form13f_first_filing_top_k=_env_int("FORM13F_FIRST_FILING_TOP_K", 5),
        form13f_max_conviction=_env_float("FORM13F_MAX_CONVICTION", 0.7),
        form13f_manager_ciks=_parse_form13f_manager_ciks(),
    )
