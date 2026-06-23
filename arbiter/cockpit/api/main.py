"""Arbiter Cockpit — read-only FastAPI sidecar (:8910).

Serves the constellation topology + live state to the R3F web client.  NEVER
writes to the trading system (see ``db.py``).  Run:

    cockpit/api  $  uvicorn main:app --port 8910 --reload
"""
from __future__ import annotations

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from .contract import Graph, Health, NodeDetail, PositionsResponse, State
from .db import connect, db_reachable
from .events import event_stream
from .graph import build_graph
from .node_detail import build_node_detail
from .positions import build_positions
from .state import _heartbeat, build_state

app = FastAPI(title="Arbiter Cockpit API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
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
