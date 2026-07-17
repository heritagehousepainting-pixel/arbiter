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
    allow_fractional: bool
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
_VALID_OPTIONS_MODES = {"off", "shadow", "paper"}

# [A3, P1] paper base url host allow-list (fail-closed).  Permit ONLY the real
# Alpaca paper host or a loopback host (tests/mocks).  Anything else — notably a
# live-money trading host — is rejected so a stray .env edit can't silently
# route "paper" orders to a live endpoint. (No live host string is named here on
# purpose: the paper-only tripwire test forbids that literal anywhere.)
_PAPER_HOST = "paper-api.alpaca.markets"
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}

# [J1, P1] Config fields whose values must be masked in repr/str.  Exact field
# names plus a suffix rule for any *_webhook_url.
_SECRET_FIELDS = {"alpaca_api_key", "alpaca_secret_key", "kill_switch_url", "anthropic_api_key"}
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
        "allow_fractional",
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
    "finnhub": {"api_key", "min_stance", "min_confidence", "catalyst_only"},
    "alerting": {"kill_switch_url", "alert_webhook_url"},
    "daemon": {"fast_interval_s", "full_cycle_times_et", "heartbeat_path"},
    "options": {
        "options_mode",
        "options_sleeve_pct",
        "option_target_delta_low",
        "option_target_delta_high",
        "option_min_expiry_days",
        "option_horizon_buffer_days",
        "option_max_expiry_buffer_days",
        "option_min_open_interest",
        "option_min_volume",
        "option_conviction_mult",
        "option_ivr_max",
        "option_breakeven_buffer_pct",
        "option_premium_stop_pct",
        "option_data_feed",
    },
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

    # Fractional-share fallback (Tier-2 #4, 2026-07-02): when the whole-share
    # floor is 0 (stock price above the position notional), size the entry as a
    # fractional qty instead of silently zero-share-skipping.  Alpaca supports
    # fractional market/limit DAY orders in paper + live (docs:
    # fractional-trading; verified 2026-07-02).  Kill switch:
    # ARBITER_ALLOW_FRACTIONAL=0.  Defaulted so existing direct ``Config(...)``
    # constructions (tests) need no change.
    allow_fractional: bool = True

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

    # Parole floor fraction (unfreeze Stage 2): a significantly-negative advisor
    # BELOW the 30-outcome mute sample trades at trust_equal_floor × this
    # fraction (default 0.5 → weight 0.125) instead of being hard-muted, so it
    # keeps accruing the outcomes that decide its fate.  Defaulted so existing
    # direct ``Config(...)`` constructions need no change.
    trust_parole_fraction: float = 0.5

    # Dedupe cooldown (2026-07-10 unfreeze): a never-executed FINAL_DECIDED idea
    # blocks its (ticker,bucket) for only this many days, then frees the slot so
    # the ticker can be reconsidered (outcome labeling still runs at full horizon).
    # Defaulted so existing direct ``Config(...)`` constructions need no change.
    dedupe_cooldown_days: int = 3

    # Stuck pre-execution sweep (2026-07-10 deadlock fix): a GATHERING or
    # PROVISIONAL_DECIDED idea stranded by a PRIOR cycle (a mid-cycle broker-fatal
    # auto-pause landed before decide) blocks its (ticker,bucket) dedupe slot
    # forever and is never re-decided.  Ideas older than this many hours in those
    # states are ABANDONED by the end-of-cycle sweep so the next cycle regenerates
    # + decides them fresh.  Must comfortably exceed one cycle's duration so a
    # legitimately in-flight current-cycle idea can never be swept; <= 0 disables
    # the sweep (fail-safe).  Env var: ARBITER_STUCK_IDEA_MAX_AGE_HOURS.
    # Defaulted so existing direct ``Config(...)`` constructions need no change.
    stuck_idea_max_age_hours: float = 2.0

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

    # A3 news weight BOOST (2026-06-23 spec) — "trust news more".  The news
    # advisor's resolved fusion weight is multiplied by a3_weight_multiplier and
    # capped at a3_weight_cap, applied on BOTH the cold-floor and graduated paths
    # but NOT on the negative-skill (suppressed) path — so a news advisor that
    # proves wrong is still reined in by the learning loop.  Default 2.0 / 0.50
    # = "strong lead, not dictator" (a consensus of other advisors can still
    # out-total it after fusion normalization).  Multiplier 1.0 disables.
    # Env vars: A3_WEIGHT_MULTIPLIER, A3_WEIGHT_CAP, A3_ADVISOR_ID.
    a3_weight_multiplier: float = 2.0

    # Tier-3 #12 (2026-07-02) — catalyst-gated A3 sweep: the engine only
    # gathers news for held tickers, fresh-signal tickers, and active-idea
    # tickers (the full 138-name sweep took 30+ min/cycle under Finnhub's
    # free tier and starved stop-checks).  False restores the full-watchlist
    # sweep (news-only discovery).  Env var: A3_CATALYST_ONLY.
    a3_catalyst_only: bool = True
    a3_weight_cap: float = 0.50
    a3_advisor_id: str = "A3.news"

    # --- Monday Refresh / A4.macro -------------------------------------
    anthropic_api_key: str = ""
    refresh_model: str = "claude-opus-4-8"
    a4_min_stance: float = 0.25
    a4_min_confidence: float = 0.0
    # Reserved for future A4.macro graduation tuning (not yet consumed — A4 is
    # intentionally held at base probationary weight, unlike A3's news boost).
    a4_weight_multiplier: float = 2.0
    a4_weight_cap: float = 0.50
    a4_advisor_id: str = "A4.macro"

    # --- Robotics early-insight signal (#3) ----------------------------
    # Model for the twice-weekly robotics web-search scan; empty falls back to
    # ``refresh_model``.  Env var: ROBOTICS_MODEL.
    robotics_model: str = ""

    # --- Robotics probationary advisor A5.robotics (#3d) ---------------
    # KILL-SWITCH — DEFAULT OFF.  The A5.robotics advisor turns twice-weekly
    # robotics trigger-hits into probationary Opinions that can NUDGE the live
    # engine.  It is dormant until the creator explicitly flips this after
    # watching the signal; even when ON it is live-only (returns [] under
    # BacktestClock), significance-gated, and weight-capped.  Env var:
    # ROBOTICS_ADVISOR_ENABLED.  Mirrors the A4.macro knobs.
    robotics_advisor_enabled: bool = False
    a5_min_stance: float = 0.25
    a5_min_confidence: float = 0.0
    # Small probationary cap: the emitted opinion's confidence is bounded by this
    # so an unproven robotics nudge can never speak as loudly as a graduated
    # advisor.  Env var: A5_WEIGHT_CAP.
    a5_weight_cap: float = 0.25
    a5_advisor_id: str = "A5.robotics"

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

    # -------------------------------------------------------------------------
    # Options expression layer (P1 shadow → P2 paper).
    # Default "off" → the entire layer is a no-op; zero behavioural change.
    # Set options_mode = "shadow" to start logging would-have-traded rows.
    # Set options_mode = "paper" (P2) to enable live Alpaca paper execution.
    # Env var: OPTIONS_MODE
    options_mode: str = "off"  # "off" | "shadow" | "paper"

    # Budget: max aggregate premium at risk as a fraction of portfolio equity.
    # 0.35 = options sleeve may hold up to 35% of portfolio value in premium.
    # NOTE: actual market exposure is still bounded by delta-adjusted notional
    # folded into the RiskBook gross/sector caps.
    # Env var: OPTIONS_SLEEVE_PCT
    options_sleeve_pct: float = 0.35

    # Contract selector: target delta band for deep-ITM selection.
    # Calls: delta ∈ [0.70, 0.80]; puts: |delta| ∈ [0.70, 0.80] (Alpaca sign-negates puts).
    # Env vars: OPTION_TARGET_DELTA_LOW, OPTION_TARGET_DELTA_HIGH
    option_target_delta_low: float = 0.70
    option_target_delta_high: float = 0.80

    # Minimum expiry in calendar days (hard floor — option must never expire
    # during the thesis holding period).
    # Env var: OPTION_MIN_EXPIRY_DAYS
    option_min_expiry_days: int = 60

    # Expiry selection: min_expiry = horizon_days + option_horizon_buffer_days
    # (ensures the option lives at least this many days beyond the thesis horizon).
    # Env var: OPTION_HORIZON_BUFFER_DAYS
    option_horizon_buffer_days: int = 30

    # Expiry selection: max_expiry = horizon_days + option_max_expiry_buffer_days
    # (caps how far out we go; avoids extremely illiquid LEAPs).
    # Env var: OPTION_MAX_EXPIRY_BUFFER_DAYS
    option_max_expiry_buffer_days: int = 180

    # Liquidity gate: open interest floor (contracts).
    # Env var: OPTION_MIN_OPEN_INTEREST
    option_min_open_interest: int = 100

    # Liquidity gate: daily volume floor (contracts).
    # Env var: OPTION_MIN_VOLUME
    option_min_volume: int = 10

    # Conviction gate multiplier: options require conviction ≥
    # equity_entry_threshold × option_conviction_mult (default 1.5×).
    # Env var: OPTION_CONVICTION_MULT
    option_conviction_mult: float = 1.5

    # IV gate: reject when IV rank > this threshold (0.40 = 40th percentile).
    # In P1 cold-start, realized vol proxy is used when IVR history is absent.
    # Env var: OPTION_IVR_MAX
    option_ivr_max: float = 0.40

    # Breakeven buffer: expected 1σ move must clear the option breakeven by
    # at least this fraction (e.g. 0.05 = 5% buffer above breakeven).
    # Env var: OPTION_BREAKEVEN_BUFFER_PCT
    option_breakeven_buffer_pct: float = 0.05

    # Premium stop: close a position when current premium ≤
    # entry_premium × (1 - option_premium_stop_pct).
    # Default 0.50 = close at −50% of premium paid.
    # Env var: OPTION_PREMIUM_STOP_PCT
    option_premium_stop_pct: float = 0.50

    # Alpaca options data feed. "indicative" is the free feed (verified working,
    # returns IV+greeks+quotes). "opra" is NOT available (no OPRA agreement).
    # Do NOT change this to "opra" — it will 403 on every snapshot call.
    # Env var: OPTION_DATA_FEED
    option_data_feed: str = "indicative"

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
    options = data.get("options", {})

    executor_backend = _env_str(
        "EXECUTOR_BACKEND", str(core.get("executor_backend", "sim"))
    )
    if executor_backend not in _VALID_EXECUTOR_BACKENDS:
        raise ConfigError(
            f"Invalid executor_backend {executor_backend!r}; "
            f"must be one of {sorted(_VALID_EXECUTOR_BACKENDS)}"
        )

    options_mode = _env_str(
        "OPTIONS_MODE", str(options.get("options_mode", "off"))
    )
    if options_mode not in _VALID_OPTIONS_MODES:
        raise ConfigError(
            f"Invalid options_mode {options_mode!r}; "
            f"must be one of {sorted(_VALID_OPTIONS_MODES)}"
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
        allow_fractional=_env_bool(
            "ARBITER_ALLOW_FRACTIONAL", bool(sizing.get("allow_fractional", True))
        ),
        dedupe_cooldown_days=_env_int("ARBITER_DEDUPE_COOLDOWN_DAYS", 3),
        stuck_idea_max_age_hours=_env_float("ARBITER_STUCK_IDEA_MAX_AGE_HOURS", 2.0),
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
        a3_weight_multiplier=_env_float("A3_WEIGHT_MULTIPLIER", float(finnhub.get("weight_multiplier", 2.0))),
        a3_catalyst_only=_env_bool("A3_CATALYST_ONLY", bool(finnhub.get("catalyst_only", True))),
        a3_weight_cap=_env_float("A3_WEIGHT_CAP", float(finnhub.get("weight_cap", 0.50))),
        a3_advisor_id=_env_str("A3_ADVISOR_ID", str(finnhub.get("advisor_id", "A3.news"))),
        anthropic_api_key=_env_str("ANTHROPIC_API_KEY", ""),
        refresh_model=_env_str("REFRESH_MODEL", "claude-opus-4-8"),
        a4_min_stance=_env_float("A4_MIN_STANCE", 0.25),
        a4_min_confidence=_env_float("A4_MIN_CONFIDENCE", 0.0),
        a4_weight_multiplier=_env_float("A4_WEIGHT_MULTIPLIER", 2.0),
        a4_weight_cap=_env_float("A4_WEIGHT_CAP", 0.50),
        a4_advisor_id=_env_str("A4_ADVISOR_ID", "A4.macro"),
        robotics_model=_env_str("ROBOTICS_MODEL", ""),
        robotics_advisor_enabled=_env_bool("ROBOTICS_ADVISOR_ENABLED", False),
        a5_min_stance=_env_float("A5_MIN_STANCE", 0.25),
        a5_min_confidence=_env_float("A5_MIN_CONFIDENCE", 0.0),
        a5_weight_cap=_env_float("A5_WEIGHT_CAP", 0.25),
        a5_advisor_id=_env_str("A5_ADVISOR_ID", "A5.robotics"),
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
        trust_parole_fraction=_env_float(
            "ARBITER_TRUST_PAROLE_FRACTION",
            float(core.get("trust_parole_fraction", 0.5)),
        ),
        form13f_min_position_usd=_env_float("FORM13F_MIN_POSITION_USD", 10_000_000.0),
        form13f_min_book_fraction=_env_float("FORM13F_MIN_BOOK_FRACTION", 0.005),
        form13f_min_delta_fraction=_env_float("FORM13F_MIN_DELTA_FRACTION", 0.25),
        form13f_first_filing_top_k=_env_int("FORM13F_FIRST_FILING_TOP_K", 5),
        form13f_max_conviction=_env_float("FORM13F_MAX_CONVICTION", 0.7),
        form13f_manager_ciks=_parse_form13f_manager_ciks(),
        # Options expression layer
        options_mode=options_mode,
        options_sleeve_pct=_env_float(
            "OPTIONS_SLEEVE_PCT", float(options.get("options_sleeve_pct", 0.35))
        ),
        option_target_delta_low=_env_float(
            "OPTION_TARGET_DELTA_LOW", float(options.get("option_target_delta_low", 0.70))
        ),
        option_target_delta_high=_env_float(
            "OPTION_TARGET_DELTA_HIGH", float(options.get("option_target_delta_high", 0.80))
        ),
        option_min_expiry_days=_env_int(
            "OPTION_MIN_EXPIRY_DAYS", int(options.get("option_min_expiry_days", 60))
        ),
        option_horizon_buffer_days=_env_int(
            "OPTION_HORIZON_BUFFER_DAYS", int(options.get("option_horizon_buffer_days", 30))
        ),
        option_max_expiry_buffer_days=_env_int(
            "OPTION_MAX_EXPIRY_BUFFER_DAYS", int(options.get("option_max_expiry_buffer_days", 180))
        ),
        option_min_open_interest=_env_int(
            "OPTION_MIN_OPEN_INTEREST", int(options.get("option_min_open_interest", 100))
        ),
        option_min_volume=_env_int(
            "OPTION_MIN_VOLUME", int(options.get("option_min_volume", 10))
        ),
        option_conviction_mult=_env_float(
            "OPTION_CONVICTION_MULT", float(options.get("option_conviction_mult", 1.5))
        ),
        option_ivr_max=_env_float(
            "OPTION_IVR_MAX", float(options.get("option_ivr_max", 0.40))
        ),
        option_breakeven_buffer_pct=_env_float(
            "OPTION_BREAKEVEN_BUFFER_PCT", float(options.get("option_breakeven_buffer_pct", 0.05))
        ),
        option_premium_stop_pct=_env_float(
            "OPTION_PREMIUM_STOP_PCT", float(options.get("option_premium_stop_pct", 0.50))
        ),
        option_data_feed=_env_str(
            "OPTION_DATA_FEED", str(options.get("option_data_feed", "indicative"))
        ),
    )
