"""Fusion engine — Lane L10.

Combines advisor opinions within each HorizonBucket into a single
FusionOutput using log-pool weights, correlation-adjusted effective-N,
and a hard-veto layer.

Public API::

    from arbiter.fusion import fuse

The ``fuse`` function is defined in ``engine.py`` and re-exported here.
"""
from __future__ import annotations

from arbiter.fusion.engine import fuse

__all__ = ["fuse"]
