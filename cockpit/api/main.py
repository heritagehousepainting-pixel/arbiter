"""Arbiter Cockpit — read-only FastAPI sidecar (:8910).

Serves the constellation topology + live state to the R3F web client.  NEVER
writes to the trading system (see ``db.py``).  Run:

    cockpit/api  $  uvicorn main:app --port 8910 --reload
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .contract import Graph, Health, IVSeries, NodeDetail, OptionsState, PositionsResponse, State, TickerDetail
from .db import connect, db_reachable
from .events import event_stream
from .graph import build_graph
from .node_detail import build_node_detail
from .options import build_iv_series, build_options_state
from .positions import build_positions
from .ticker import build_ticker_detail
from .state import _heartbeat, build_state

app = FastAPI(title="Arbiter Cockpit API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    # Vite picks the first free port (5173, then 5174, 5175, ...), so allow any
    # localhost dev port rather than pinning one that may be taken by another app.
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
    allow_methods=["GET"],
    allow_headers=["*"],
)


@app.get("/health", response_model=Health)
def health() -> Health:
    return Health(db=db_reachable(), daemon=_heartbeat() is not None, alpaca=False)


@app.get("/graph", response_model=Graph)
def graph() -> Graph:
    conn = connect()
    try:
        return build_graph(conn)
    finally:
        conn.close()


@app.get("/state", response_model=State)
def state() -> State:
    conn = connect()
    try:
        return build_state(conn)
    finally:
        conn.close()


# Route wired to the foundation; Lane 1 owns node_detail.py, Lane 2 owns events.py.
@app.get("/node/{node_id}", response_model=NodeDetail)
def node_detail(node_id: str) -> NodeDetail:
    conn = connect()
    try:
        detail = build_node_detail(conn, node_id)
    finally:
        conn.close()
    if detail is None:
        raise HTTPException(status_code=404, detail=f"unknown node {node_id}")
    return detail


@app.get("/events")
def events() -> StreamingResponse:
    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.get("/positions", response_model=PositionsResponse)
def positions() -> PositionsResponse:
    """Live open positions + portfolio stats (read-only, from Alpaca)."""
    return build_positions()


@app.get("/ticker/{symbol}", response_model=TickerDetail)
def ticker_detail(symbol: str) -> TickerDetail:
    """Company name + 1-month return for one ticker (lazy, per-expand, read-only)."""
    return build_ticker_detail(symbol.strip().upper())


@app.get("/options", response_model=OptionsState)
def options() -> OptionsState:
    """Complete options snapshot — mode, open positions, shadow plays, outcomes, aggregates."""
    conn = connect()
    try:
        return build_options_state(conn)
    finally:
        conn.close()


@app.get("/options/iv/{ticker}", response_model=IVSeries)
def options_iv(ticker: str) -> IVSeries:
    """ATM-IV history for one underlying ticker.  Returns empty series (never 404)."""
    conn = connect()
    try:
        return build_iv_series(conn, ticker)
    finally:
        conn.close()
