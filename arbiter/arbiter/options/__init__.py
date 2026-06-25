"""Options expression layer for Arbiter (P1 shadow → P2 paper).

This package adds a long-dated long-call/put overlay on high-conviction
directional theses already produced by the A1/A2/A3 council.  It is a
post-``decide`` overlay: it reads the same ``fusion_output`` + ``idea`` that
the equity path uses and never alters equity behaviour.

Gated by ``config.options_mode``:
  "off"    — (default) the entire layer is a no-op; zero behavioural change.
  "shadow" — gate + contract selection + sizing run; results written to
             ``option_shadow_log`` only; ``place()`` raises NotImplementedError.
  "paper"  — same as shadow plus live Alpaca paper execution.

Public entry point (called by ``engine/_engine.py`` after equity submit)::

    from arbiter.options.express import express_option
    express_option(conn, idea, fusion_output, config=config,
                   risk_book=book, clock=clock)

Sub-modules (all stubs in P1 foundation; parallel wave fills bodies):
  types.py                — frozen dataclasses + enums (no logic)
  gate.py                 — options_expression_gate()
  contract_selector.py    — select_contract()
  sizing.py               — size_option()
  shadow_log.py           — log_shadow_option()
  alpaca_options_client.py — AlpacaOptionsClient (chains, snapshots, orders)
  iv_history.py           — record_iv_snapshot(), iv_rank(), realized_vol_proxy()
  outcomes.py             — record_option_outcome() (ISOLATED from equity outcomes)
  exit.py                 — premium_stop_exit() (P2)
  express.py              — express_option() orchestrator
"""
from __future__ import annotations
