"""MiroFish adapter (A2) — Lane 7.

MiroFish is a self-hosted quantitative reality-check engine.  It is called
over local HTTP **only** — never imported directly (AGPL, INTERFACES.md §11.5).

Public entry point::

    from arbiter.adapters.mirofish.adapter import run

Shadow/stub mode: the adapter is always in shadow (weight=0, recorded only)
until the self-hosted MiroFish service is reachable.  Weight promotion is
handled by Lane 11 (trust ledger).

Hard weight cap: 0.35 forever (INTERFACES.md §5).
Advisor ID: ``"A2.mirofish"``.
"""
from __future__ import annotations

from arbiter.adapters.mirofish.adapter import ADVISOR_ID, run

__all__ = ["ADVISOR_ID", "run"]
