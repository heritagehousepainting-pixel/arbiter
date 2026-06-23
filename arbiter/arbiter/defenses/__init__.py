"""Anti-manipulation defenses — Lane 8 (safety dependency).

This package contains defenses that other lanes consume as safety gates.
The primary export is ``VolumeAnomalyGate``, which is a cross-lane dependency:

Lane 4 (safety/breakers.py)
    ``check_a3_volume_anomaly()`` calls ``VolumeAnomalyGate.is_anomalous()``
    to determine whether to trip the ``a3_volume_anomaly`` latching breaker.

Lane 13 (orchestrator sweep)
    The idea-creation sweep checks ``VolumeAnomalyGate.is_anomalous()`` before
    accepting new tips on a held name to prevent manipulation of open positions.

Wave-C wiring
-------------
These gates are *wired but dormant* in Phase-6 MVP: the gate code runs and
returns results, but no live Opinion is produced by the tips layer.  The
``VolumeAnomalyGate`` is the exception — it is an active safety dependency and
is consumed by Lane 4 regardless of tips layer shadow status.

See ``arbiter/defenses/volume_anomaly.py`` for the gate implementation.
"""
from __future__ import annotations

from arbiter.defenses.volume_anomaly import VolumeAnomalyGate

__all__ = ["VolumeAnomalyGate"]
