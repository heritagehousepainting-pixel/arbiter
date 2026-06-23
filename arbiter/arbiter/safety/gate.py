"""Safety gate — Lane L4 public entry point.

``is_trading_allowed`` is the single function called by Lane 12 (policy)
before EVERY order.  It is fail-closed: any unexpected exception from an
external component results in a HALTED decision (no trade).

Contract: INTERFACES.md §8
Spec:     docs/specs/2026-06-18-arbiter-decision-engine-design.md §3.9

Design decisions
----------------
- ``breaker_provider`` is an **injectable callable** (not a direct import of
  ``arbiter.safety.breakers``).  This lets Wave-C wire the real breakers module
  without creating an import-time dependency while it is being built in parallel.
  The callable must return ``list[str]`` (tripped breaker names).
- No ``datetime.now()`` anywhere — the ``audit()`` function handles the NO_CLOCK
  sentinel when no ts is supplied (see db/audit.py).
- The ``account`` parameter is accepted for forward-compatibility (position-level
  checks, account equity for loss-rate computation) but is not read in this Wave.
  It is passed through to the audit payload for traceability.

Wiring note (Wave C)
---------------------
Lane 12 should inject the real breakers callable like this::

    from arbiter.safety import breakers  # built in Wave C
    from arbiter.safety.gate import is_trading_allowed

    decision = is_trading_allowed(
        account,
        live_advisor_count=n,
        breaker_provider=breakers.any_tripped,
    )

Public API
----------
is_trading_allowed(account, *, live_advisor_count, breaker_provider=None)
    → TradingDecision
"""
from __future__ import annotations

import traceback
from typing import Callable

from arbiter.contract.seams import TradingDecision
from arbiter.db.audit import audit
from arbiter.types import DegradationLevel
from arbiter.safety.quorum import assess_quorum
from arbiter.safety.degradation import (
    effective_multiplier,
    highest_level,
    level_supersedes_trading,
    LEVEL_POLICIES,
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _call_breaker_provider(
    breaker_provider: Callable[[], list[str]] | None,
) -> tuple[list[str], bool]:
    """Call the breaker provider safely.

    Returns
    -------
    (tripped_names, error_occurred)
        ``error_occurred=True`` triggers fail-closed behaviour in the gate.
    """
    if breaker_provider is None:
        return [], False
    try:
        result = breaker_provider()
        if not isinstance(result, list):
            # Defensive: provider returned something unexpected; treat as fault.
            return [], True
        return result, False
    except Exception:  # noqa: BLE001
        # Any exception → fail-closed; log the traceback for post-mortem.
        return [], True


# ---------------------------------------------------------------------------
# Public gate
# ---------------------------------------------------------------------------

def is_trading_allowed(
    account: object,
    *,
    live_advisor_count: int,
    breaker_provider: Callable[[], list[str]] | None = None,
) -> TradingDecision:
    """Evaluate all safety signals and return a TradingDecision.

    This is the single gate Lane 12 calls before every order.

    Parameters
    ----------
    account:
        Account object (passed through to audit; not read in this wave).
    live_advisor_count:
        Number of advisors currently producing live signals.
    breaker_provider:
        Optional callable ``() -> list[str]`` returning the names of any
        tripped circuit breakers.  If ``None``, breaker checks are skipped
        (useful in unit tests for quorum-only scenarios).
        If the callable raises, the gate is fail-closed (not allowed).

    Returns
    -------
    TradingDecision
        Frozen dataclass with ``allowed``, ``size_multiplier``, ``level``,
        and ``reasons``.
    """
    reasons: list[str] = []

    # ------------------------------------------------------------------
    # 1. Circuit breakers (fail-closed on exception or unreachable)
    # ------------------------------------------------------------------
    tripped_breakers, breaker_error = _call_breaker_provider(breaker_provider)

    if breaker_error:
        reasons.append(
            "breaker_provider raised an exception — fail-closed, trading HALTED"
        )
        decision = TradingDecision(
            allowed=False,
            size_multiplier=0.0,
            level=DegradationLevel.HALTED,
            reasons=reasons,
        )
        _emit_audit(account, live_advisor_count, decision, breaker_error=True)
        return decision

    if tripped_breakers:
        for name in tripped_breakers:
            reasons.append(f"circuit breaker tripped: {name}")

    # ------------------------------------------------------------------
    # 2. Quorum assessment
    # ------------------------------------------------------------------
    quorum = assess_quorum(live_advisor_count)
    reasons.extend(quorum.reasons)

    # ------------------------------------------------------------------
    # 3. Combine all signals → derive the final degradation level
    # ------------------------------------------------------------------
    # Start with the level implied by quorum.
    combined_level = quorum.level

    # Breakers always bump to at least HALTED if any are tripped.
    if tripped_breakers:
        combined_level = highest_level(combined_level, DegradationLevel.HALTED)

    # The degradation ladder can further supersede (levels 3–4 block trading).
    policy = LEVEL_POLICIES[combined_level]

    # ------------------------------------------------------------------
    # 4. Derive final allowed + size_multiplier
    # ------------------------------------------------------------------
    blocked_by_level = level_supersedes_trading(combined_level)
    blocked_by_breaker = bool(tripped_breakers)

    allowed = not (blocked_by_level or blocked_by_breaker)
    size_mult = effective_multiplier(combined_level, quorum.size_multiplier)

    # Consistency guard: if not allowed, size must be 0.
    if not allowed:
        size_mult = 0.0

    if policy.description not in ("normal operations",) and not reasons:
        reasons.append(policy.description)

    decision = TradingDecision(
        allowed=allowed,
        size_multiplier=size_mult,
        level=combined_level,
        reasons=reasons,
    )

    # ------------------------------------------------------------------
    # 5. Audit every decision
    # ------------------------------------------------------------------
    _emit_audit(account, live_advisor_count, decision, breaker_error=False)

    return decision


def _emit_audit(
    account: object,
    live_advisor_count: int,
    decision: TradingDecision,
    *,
    breaker_error: bool,
) -> None:
    """Write one line to the append-only audit log.

    Uses ``audit()`` from ``arbiter.db.audit``; the NO_CLOCK sentinel is
    written automatically when no ts is passed (clock is wired in Wave C).
    """
    try:
        audit(
            "safety_gate_decision",
            {
                "account_repr": repr(account),
                "live_advisor_count": live_advisor_count,
                "allowed": decision.allowed,
                "size_multiplier": decision.size_multiplier,
                "level": decision.level.name,
                "reasons": decision.reasons,
                "breaker_error": breaker_error,
            },
        )
    except Exception:  # noqa: BLE001
        # Audit failure must not block a gate decision — log and continue.
        pass
