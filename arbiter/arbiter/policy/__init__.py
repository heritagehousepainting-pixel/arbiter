"""Decision policy and sizing — Lane 12a.

Public surface::

    from arbiter.policy.sizing import compute_size
    from arbiter.policy.exits import compute_exits
    from arbiter.policy.decision import decide
"""
from __future__ import annotations

from arbiter.policy.sizing import compute_size
from arbiter.policy.exits import compute_exits
from arbiter.policy.decision import decide

__all__ = ["compute_size", "compute_exits", "decide"]
