"""Adapters package — external advisor integrations for Arbiter.

Each sub-package wraps one external signal source (MiroFish, A3, etc.)
and exposes a single ``run(idea, as_of) -> list[Opinion]`` entry point.

AGPL isolation rule (INTERFACES.md §11.5):
    No adapter may ``import`` an AGPL library directly. All AGPL tools
    must be called over local HTTP only.
"""
from __future__ import annotations
