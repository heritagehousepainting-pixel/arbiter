"""Tiered alerting for Arbiter (Lane 4c).

Tiers
-----
info     → audit log only.
warning  → audit log only (higher severity label).
critical → audit log + POST to ``Config.alert_webhook_url`` + signal auto-pause.

All ``alert()`` calls accept an ``as_of`` timestamp (tz-aware UTC datetime)
which is passed to the audit log.  No ``datetime.now()`` (INTERFACES.md §11.1).

Auto-pause sentinel
-------------------
``alert()`` at ``critical`` tier returns an ``AutoPauseSentinel`` instance.
The engine must check the return value and treat a non-``None`` return as a
request to pause order submission.  This is a *signal* — it does not itself
stop any threads; that is the engine's responsibility (Wave-C wiring point).

Webhook contract
----------------
    POST <alert_webhook_url>
    Content-Type: application/json
    Body: {"tier": "critical", "message": "...", "ctx": {...}, "as_of": "..."}

Network failures on the webhook POST are logged but do NOT suppress the
``AutoPauseSentinel`` — the auto-pause fires regardless of delivery.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

import httpx
import structlog

from arbiter.config import Config
from arbiter.db.audit import audit as _audit_write

log = structlog.get_logger(__name__)

AlertTier = Literal["info", "warning", "critical"]

_HTTP_TIMEOUT: float = 5.0


# ---------------------------------------------------------------------------
# Sentinel
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AutoPauseSentinel:
    """Returned by ``alert()`` when ``tier == "critical"``.

    The engine checks ``result = alert(...)`` and if it is an
    ``AutoPauseSentinel`` it should pause new order submission immediately.

    Attributes
    ----------
    message:
        Human-readable reason for the pause.
    tier:
        Always ``"critical"`` (preserved for downstream filtering).
    """
    message: str
    tier: str = "critical"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

@dataclass
class Alerting:
    """Tiered alerting with audit-log integration and webhook delivery.

    Parameters
    ----------
    config:
        Frozen ``Config``; ``config.alert_webhook_url`` is the POST target.
    audit_path:
        Override the audit log path (for tests).  If ``None``, the default
        from ``Config.audit_path`` is used.
    http_timeout:
        Seconds to wait for the webhook POST.

    Notes
    -----
    The caller is responsible for holding the ``Alerting`` instance and
    calling ``alert()`` from the engine's main loop.
    """

    config: Config
    audit_path: str | None = None
    http_timeout: float = _HTTP_TIMEOUT

    def alert(
        self,
        tier: AlertTier,
        message: str,
        ctx: dict[str, Any],
        *,
        as_of: datetime,
    ) -> AutoPauseSentinel | None:
        """Fire an alert at the given tier.

        Parameters
        ----------
        tier:
            ``"info"`` | ``"warning"`` | ``"critical"``.
        message:
            Human-readable description of the event.
        ctx:
            Arbitrary key/value context dict (ticker, reason, values, …).
        as_of:
            Logical timestamp (tz-aware UTC).  Written to the audit log; must
            not be ``datetime.now()`` at the call site.

        Returns
        -------
        AutoPauseSentinel | None
            ``AutoPauseSentinel`` when ``tier == "critical"``; ``None``
            otherwise.  The engine must treat a non-``None`` return as a
            signal to pause new order submission.
        """
        ts = as_of.isoformat()

        # ------------------------------------------------------------------
        # 1. Write to audit log (all tiers).
        # ------------------------------------------------------------------
        _audit_write(
            f"alert.{tier}",
            {"message": message, "tier": tier, "ctx": ctx},
            ts=ts,
            audit_path=self.audit_path,
        )

        log.bind(tier=tier, message=message, ctx=ctx).info("alert.fired")

        # ------------------------------------------------------------------
        # 2. Webhook POST + auto-pause sentinel (critical only).
        # ------------------------------------------------------------------
        if tier == "critical":
            self._post_webhook(tier=tier, message=message, ctx=ctx, ts=ts)
            return AutoPauseSentinel(message=message)

        return None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _post_webhook(
        self,
        *,
        tier: str,
        message: str,
        ctx: dict[str, Any],
        ts: str,
    ) -> None:
        """POST the alert to the configured webhook URL.

        Network failures are logged but do not propagate — the ``AutoPauseSentinel``
        is returned regardless of delivery success (fire-and-forget delivery).
        """
        url = self.config.alert_webhook_url
        if not url:
            log.warning("alerting.no_webhook_url", tier=tier)
            return

        payload = {
            "tier": tier,
            "message": message,
            "ctx": ctx,
            "as_of": ts,
        }
        try:
            response = httpx.post(url, json=payload, timeout=self.http_timeout)
            response.raise_for_status()
            log.info("alerting.webhook_delivered", tier=tier, status=response.status_code)
        except (httpx.RequestError, httpx.HTTPStatusError) as exc:
            # Delivery failure is recorded but does NOT suppress auto-pause.
            log.error("alerting.webhook_failed", error=str(exc), url=url, tier=tier)
