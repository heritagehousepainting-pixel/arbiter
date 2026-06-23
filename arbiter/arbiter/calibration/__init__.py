"""Calibration layer — Lane 9 (arbiter/calibration/).

Owns the raw-stance → probability transform.  Fusion (Lane 10) consumes
:class:`Calibrator` via its ``calibrator`` parameter.

Sub-modules
-----------
stance_base : STANCE_BASE hard-coded cold-start prior table.
platt       : Platt (logistic) scaling; used when < 200 resolved outcomes.
isotonic    : Isotonic regression; used when >= 200 resolved outcomes.
calibrator  : ``Calibrator`` — the seam fusion consumes.
"""
from __future__ import annotations

from arbiter.calibration.calibrator import Calibrator

__all__ = ["Calibrator"]
