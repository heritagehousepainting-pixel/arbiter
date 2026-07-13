"""Render + push the robotics early-insight digest (phone)."""
from __future__ import annotations

from typing import Any

from arbiter.robotics_signal.types import RoboticsReport


def build_digest(report: RoboticsReport) -> str:
    d = report.as_of.date().isoformat()
    scan = report.scan
    lines = [f"# 🤖 ROBOTICS SIGNAL — {d}", ""]

    hits = scan.trigger_hits
    lines.append(f"## ⭐ TRIGGER HITS ({len(hits)})")
    if not scan.available:
        lines.append(f"- scan {scan.note}")
    elif not hits:
        lines.append("- no watch-triggers fired")
    else:
        for h in hits:
            lines.append(f"- ⭐ {h.trigger_name}: {h.headline}")
    lines.append("")

    other = [d for d in scan.developments if not d.trigger_hit]
    lines.append(f"## DEVELOPMENTS ({len(other)})")
    if scan.available and not other:
        lines.append("- nothing else notable")
    for dv in other:
        tick = f" [{', '.join(dv.symbols)}]" if dv.symbols else ""
        lines.append(f"- ({dv.category}) {dv.headline}{tick}")
    return "\n".join(lines)


def _headline(report: RoboticsReport) -> str:
    scan = report.scan
    if not scan.available:
        return f"scan {scan.note}"
    return (f"{len(scan.trigger_hits)} trigger hit(s) · "
            f"{len(scan.developments)} development(s)")


def push_digest(report: RoboticsReport, *, alerting: Any) -> None:
    """Fire-and-forget phone push via the shared Alerting webhook seam."""
    alerting.notify("Robotics Signal", _headline(report), as_of=report.as_of)
