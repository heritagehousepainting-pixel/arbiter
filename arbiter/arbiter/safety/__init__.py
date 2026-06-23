"""Safety package public surface — Lane L4 (Wave C re-export shim).

Re-exports the three key public names that the composition root and policy
layers need:

    ``is_trading_allowed``  — the single gate callable (see gate.py)
    ``CircuitBreaker``      — latching breaker registry (see breakers.py)
    ``KillSwitch``          — broker-side kill switch (see kill_switch.py)
    ``Alerting``            — tiered alerting (see alerting.py)

``CircuitBreaker.reset()`` is intentionally NOT re-exported here so that
advisor and fusion layers that do ``from arbiter.safety import *`` cannot
reach it through normal import paths (INTERFACES.md §8 / breakers.py note).
Admin code that must call ``reset()`` must import directly::

    from arbiter.safety.breakers import CircuitBreaker
"""
from __future__ import annotations

from arbiter.safety.gate import is_trading_allowed
from arbiter.safety.breakers import CircuitBreaker
from arbiter.safety.kill_switch import KillSwitch
from arbiter.safety.alerting import Alerting

__all__ = [
    "is_trading_allowed",
    "CircuitBreaker",
    "KillSwitch",
    "Alerting",
]
