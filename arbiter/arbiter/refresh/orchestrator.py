"""Monday Refresh orchestrator — runs scans, feeds engine, pushes digest."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

import structlog

from arbiter.refresh.digest import build_digest, push_digest
from arbiter.refresh.findings_store import persist_findings
from arbiter.refresh.macro_scan import scan_macro
from arbiter.refresh.position_news import scan_position_news
from arbiter.refresh.source_health import merge_flags, scan_source_health
from arbiter.refresh.types import RefreshReport

log = structlog.get_logger(__name__)


def _safe(label: str, fn: Callable[[], Any], default: Any) -> Any:
    try:
        return fn()
    except Exception as exc:  # belt-and-suspenders; scans are already fail-closed
        log.warning("refresh.step_failed", step=label, error=str(exc))
        return default


def run_monday_refresh(engine: Any, *, llm: Any = None, finnhub: Any = None,
                       ingest_fn: Callable[..., Any] | None = None,
                       alerting: Any = None) -> RefreshReport:
    as_of = engine.clock.now()
    tickers = sorted(engine.executor.get_positions().keys())

    if finnhub is None:
        from arbiter.ingest.finnhub.client import FinnhubClient  # noqa: PLC0415
        key = getattr(engine.config, "finnhub_api_key", "") or ""
        finnhub = FinnhubClient(key) if key else None

    positions = _safe("position_news",
                      lambda: scan_position_news(tickers, as_of, finnhub) if finnhub else [],
                      [])
    macro = _safe("macro", lambda: scan_macro(tickers, as_of, engine.config, llm=llm),
                  scan_macro([], as_of, engine.config, llm=None))
    health = _safe("health", lambda: scan_source_health(engine.conn, as_of), None)
    if health is None:
        from arbiter.refresh.types import HealthResult  # noqa: PLC0415
        health = HealthResult(sources=[])
    health = merge_flags(health, macro.stale_flags)

    # --- feed engine: persist macro findings (engine's A4 gather reads them) ---
    fed: list[str] = []
    def _persist() -> None:
        n = persist_findings(engine.conn, macro.findings, as_of)
        for f in macro.findings:
            fed.extend(f.affected_tickers)
        log.info("refresh.findings_persisted", rows=n)
    _safe("persist_findings", _persist, None)

    # --- feed engine: targeted re-ingest of confirmed-stale sources ---
    reingested: list[str] = []
    if ingest_fn is None:
        from arbiter.ingest import run_ingest  # noqa: PLC0415
        ingest_fn = lambda **kw: run_ingest(engine.config, conn=engine.conn,
                                            clock=lambda: as_of.isoformat(), **kw)
    for src in health.confirmed_stale():
        if src.source in {"form4", "form13d", "form13f", "congress"}:
            ok = _safe(f"reingest_{src.source}",
                       lambda s=src.source: ingest_fn(sources=[s]) or True, None)
            if ok:
                reingested.append(src.source)

    report = RefreshReport(as_of=as_of, positions=positions, macro=macro,
                           health=health, fed_tickers=sorted(set(fed)),
                           reingested=reingested)

    # --- digest: save + push (always) ---
    md = build_digest(report)
    out = Path("data") / f"monday-refresh-{as_of.date().isoformat()}.md"
    _safe("save_digest", lambda: out.write_text(md, encoding="utf-8"), None)
    if alerting is None:
        from arbiter.safety.alerting import Alerting  # noqa: PLC0415
        alerting = Alerting(config=engine.config,
                            audit_path=getattr(engine.config, "audit_path", "data/audit.jsonl"))
    _safe("push_digest", lambda: push_digest(report, alerting=alerting), None)
    return report
