"""Render + push the Monday Refresh digest."""
from __future__ import annotations

from typing import Any

from arbiter.refresh.types import RefreshReport


def build_digest(report: RefreshReport) -> str:
    d = report.as_of.date().isoformat()
    lines = [f"# 📋 MONDAY REFRESH — {d}", ""]

    lines.append("## MARKETS")
    if not report.macro.available:
        lines.append(f"- macro scan {report.macro.note}")
    elif not report.macro.findings:
        lines.append("- no market-moving items flagged")
    else:
        for f in report.macro.findings:
            tick = f", affects {', '.join(f.affected_tickers)}" if f.affected_tickers else ""
            lines.append(f"- [{f.severity.value}] {f.summary}{tick}")
    lines.append("")

    lines.append(f"## OPEN TRADES ({len(report.positions)})")
    for p in report.positions:
        if not p.available:
            lines.append(f"- {p.ticker}: news unavailable")
        elif p.headlines:
            mark = "⚠" if p.severity == p.severity.HIGH else "•"
            lines.append(f"- {mark} {p.ticker} ({p.sentiment:+.2f}): {p.headlines[0]}")
        else:
            lines.append(f"- ✓ {p.ticker}: nominal")
    lines.append("")

    lines.append("## DATA SOURCES")
    stale = report.health.confirmed_stale()
    if not stale:
        lines.append("- all sources nominal")
    for s in stale:
        lines.append(f"- ⚠ {s.source}: {s.reason}")
    lines.append("")

    lines.append("## ACTIONS")
    lines.append(f"- fed engine (A4.macro): {', '.join(report.fed_tickers) or 'none'}")
    lines.append(f"- re-ingested: {', '.join(report.reingested) or 'none'}")
    return "\n".join(lines)


def _headline(report: RefreshReport) -> str:
    n_macro = len(report.macro.findings)
    n_stale = len(report.health.confirmed_stale())
    return (f"{len(report.positions)} positions · {n_macro} macro item(s) · "
            f"{n_stale} stale source(s)")


def push_digest(report: RefreshReport, *, alerting: Any) -> None:
    alerting.notify("Monday Refresh", _headline(report), as_of=report.as_of)
