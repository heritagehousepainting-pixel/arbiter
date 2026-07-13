"""Arbiter CLI — entrypoint for all subcommands.

Wave-C: subcommands are now wired to the composition root (engine.py).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import typer

from arbiter.logging_setup import configure_logging

app = typer.Typer(
    name="arbiter",
    help="Smart-money decision engine.",
    no_args_is_help=True,
)


@app.callback()
def _setup(
    log_level: str = typer.Option("INFO", "--log-level", help="Logging level"),
) -> None:
    """Global setup run before any subcommand."""
    configure_logging(level=log_level)


@app.command("run-cycle")
def run_cycle(
    as_of: str = typer.Option("", "--as-of", help="ISO timestamp for the cycle (default: now)"),
) -> None:
    """Run one decision cycle (fuse → decide → execute).

    NOTE: this places REAL orders on whatever ``EXECUTOR_BACKEND`` selects
    (``alpaca_paper`` = real paper-broker orders). To preview WITHOUT placing
    orders, set ``EXECUTOR_BACKEND=sim`` first. (There is intentionally no
    ``--dry-run`` flag: a prior one was a no-op that did NOT suppress orders.)
    """
    # Import lazily so the CLI stays import-safe even before lanes are wired.
    from arbiter.engine import build_engine  # noqa: PLC0415

    engine = build_engine()

    ts: datetime | None = None
    if as_of:
        ts = datetime.fromisoformat(as_of)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

    result = engine.run_cycle(as_of=ts)

    # Honest mode label: reflect the executor backend (the real broker selector),
    # not live_trading (which is display-only and always False on paper).
    backend = engine.config.executor_backend
    mode = "PAPER (alpaca_paper) — REAL paper orders" if backend == "alpaca_paper" else "SIM (no real orders)"
    typer.echo(f"mode: {mode}")
    typer.echo(f"Cycle complete.")
    typer.echo(f"  ideas_processed  : {result.ideas_processed}")
    typer.echo(f"  orders_submitted : {result.orders_submitted}")
    typer.echo(f"  opinions_gathered: {result.opinions_gathered}")
    typer.echo(f"  opinions_null    : {result.opinions_null}")
    if result.errors:
        typer.echo(f"  errors           : {result.errors}")

    # Confirm audit was written.
    from arbiter.db.audit import read_audit  # noqa: PLC0415
    entries = read_audit()
    typer.echo(f"  audit.jsonl entries: {len(entries)}")


@app.command("leaderboard")
def leaderboard(
    limit: int = typer.Option(20, "--limit", "-n", help="Number of rows to show"),
    as_of: str = typer.Option("", "--as-of", help="ISO timestamp (default: now)"),
) -> None:
    """Print the advisor signal leaderboard."""
    from arbiter.engine import build_engine  # noqa: PLC0415

    engine = build_engine()

    ts: datetime | None = None
    if as_of:
        ts = datetime.fromisoformat(as_of)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)

    board = engine.leaderboard(as_of=ts)
    typer.echo(board)


@app.command("status")
def status() -> None:
    """Print system status (circuit breakers, degradation level, open positions)."""
    from arbiter.engine import build_engine  # noqa: PLC0415

    engine = build_engine()
    info = engine.status()

    typer.echo("Arbiter Engine Status")
    typer.echo("=" * 40)
    typer.echo(f"  live_trading     : {info['live_trading']}")
    typer.echo(f"  executor         : {info['executor']}")
    typer.echo(f"  is_sim           : {info['is_sim']}")
    typer.echo(f"  tripped_breakers : {info['tripped_breakers'] or 'none'}")
    typer.echo(f"  open_positions   : {info['open_positions']}")
    typer.echo(f"  advisor_count    : {info['advisor_count']}")
    typer.echo(f"  advisors         : {', '.join(info['advisors'])}")
    typer.echo(f"  account_equity   : {info['account_equity']}")
    typer.echo(f"  account_cash     : {info['account_cash']}")


@app.command("ingest")
def ingest(
    sources: str = typer.Option("form4,congress", "--sources", help="Comma-separated: form4,congress"),
    tickers: str = typer.Option("", "--tickers", help="Comma-separated tickers (default: built-in watchlist)"),
    lookback_days: int = typer.Option(7, "--lookback-days", help="How many days back to pull"),
) -> None:
    """Pull real SEC Form 4 + Congress disclosures into the DB."""
    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.ingest import run_ingest  # noqa: PLC0415

    engine = build_engine()
    src = tuple(s.strip() for s in sources.split(",") if s.strip())
    tks = [t.strip().upper() for t in tickers.split(",") if t.strip()] or None

    summary = run_ingest(
        engine.config,
        conn=engine.conn,
        clock=lambda: engine.clock.now().isoformat(),
        sources=src,
        tickers=tks,
        lookback_days=lookback_days,
    )
    typer.echo("Ingest complete.")
    typer.echo(f"  sources : {', '.join(summary.sources)}")
    typer.echo(f"  fetched : {summary.n_fetched}")
    typer.echo(f"  written : {summary.n_written}")
    typer.echo(f"  skipped : {summary.n_skipped}")
    for note in getattr(summary, "notes", []):
        typer.echo(f"  note    : {note}")
    if summary.errors:
        typer.echo(f"  errors  : {len(summary.errors)} (first: {summary.errors[0]})")


@app.command("run")
def run() -> None:
    """Run one scheduled iteration: ingest then a decision cycle (cron/launchd entrypoint)."""
    from arbiter.orchestrator.loop_runner import main as loop_main  # noqa: PLC0415

    report = loop_main()
    typer.echo("Scheduled run complete.")
    typer.echo(f"  ingest_ok    : {getattr(report, 'ingest_ok', 'n/a')}")
    if getattr(report, "ingest_error", None):
        typer.echo(f"  ingest_error : {report.ingest_error}")
    cr = getattr(report, "cycle_result", None)
    if cr is not None:
        typer.echo(f"  orders_submitted : {getattr(cr, 'orders_submitted', 'n/a')}")
        typer.echo(f"  opinions_gathered: {getattr(cr, 'opinions_gathered', 'n/a')}")


@app.command("daemon")
def daemon() -> None:
    """Run the market-hours intraday runtime daemon (launchd KeepAlive entrypoint).

    Loops while the US market is open: a cheap fast iteration (reconcile + stop /
    horizon checks against the LIVE price) every ARBITER_FAST_INTERVAL_S, and a
    full cycle (ingest + entries + reversal) at ARBITER_FULL_CYCLE_TIMES_ET.
    Long-sleeps while closed; SIGTERM/SIGINT exits gracefully.
    """
    from arbiter.runtime.daemon import main as daemon_main  # noqa: PLC0415

    typer.echo("Starting arbiter daemon (Ctrl-C / SIGTERM to stop)...")
    state = daemon_main()
    typer.echo("Daemon stopped.")
    typer.echo(f"  last_ingest_date : {getattr(state, 'last_ingest_date', None)}")


@app.command("monday-refresh")
def monday_refresh() -> None:
    """Run the Monday pre-market intelligence pass (scan + digest + feed engine)."""
    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.refresh.orchestrator import run_monday_refresh  # noqa: PLC0415

    engine = build_engine()
    report = run_monday_refresh(engine)
    typer.echo("Monday refresh complete.")
    typer.echo(f"  positions scanned : {len(report.positions)}")
    typer.echo(f"  macro findings    : {len(report.macro.findings)} "
               f"(available={report.macro.available})")
    typer.echo(f"  stale sources     : {len(report.health.confirmed_stale())}")
    typer.echo(f"  fed (A4.macro)    : {', '.join(report.fed_tickers) or 'none'}")
    typer.echo(f"  re-ingested       : {', '.join(report.reingested) or 'none'}")


@app.command("robotics-scan")
def robotics_scan() -> None:
    """Run the robotics early-insight scan (web search → phone digest)."""
    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.robotics_signal.orchestrator import run_robotics_scan  # noqa: PLC0415

    engine = build_engine()
    report = run_robotics_scan(engine)
    typer.echo("Robotics scan complete.")
    typer.echo(f"  available    : {report.scan.available}")
    typer.echo(f"  developments : {len(report.scan.developments)}")
    typer.echo(f"  trigger hits : {len(report.scan.trigger_hits)}")


@app.command("backtest")
def backtest(
    start: str = typer.Option(..., "--start", help="Start date YYYY-MM-DD"),
    end: str = typer.Option(..., "--end", help="End date YYYY-MM-DD"),
) -> None:
    """Run a point-in-time backtest over a date range."""
    from datetime import date  # noqa: PLC0415
    from arbiter.evaluation.backtest.runner import main as backtest_main  # noqa: PLC0415

    report = backtest_main(start=date.fromisoformat(start), end=date.fromisoformat(end))
    typer.echo(report.render())


@app.command("backfill")
def backfill(
    as_of: str = typer.Option("", "--as-of", help="ISO cutoff ('now'); default: engine clock"),
) -> None:
    """Mint historical outcomes from already-ingested filings (W-BACKFILL).

    Replays past disclosures whose horizon has elapsed (relative to --as-of)
    through the PIT-clean signal→opinion→idea→outcome pipeline so the trust
    ledger / calibrator have CLOSED-outcome volume now instead of after a
    ~1-year cold start.
    """
    from arbiter.engine import build_engine  # noqa: PLC0415
    from arbiter.evaluation.backfill import backfill_outcomes  # noqa: PLC0415

    engine = build_engine()

    if as_of:
        cutoff = datetime.fromisoformat(as_of)
        if cutoff.tzinfo is None:
            cutoff = cutoff.replace(tzinfo=timezone.utc)
    else:
        cutoff = engine.clock.now()

    report = backfill_outcomes(
        engine.conn,
        engine.pit,
        cutoff_as_of=cutoff,
        audit_path=engine.config.audit_path,
    )
    typer.echo(report.render())


@app.command("dashboard")
def dashboard(
    port: int = typer.Option(8798, "--port", "-p", help="Port to serve on"),
) -> None:
    """Serve the read-only web dashboard (Ctrl-C to stop)."""
    from arbiter.web.server import serve  # noqa: PLC0415

    typer.echo(f"Serving dashboard on http://127.0.0.1:{port}  (Ctrl-C to stop)")
    serve(port)


if __name__ == "__main__":
    app()
