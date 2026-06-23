"""Execution layer — Lane 12b.

Public surface:
    - idempotency: dedup_hash, ensure_not_duplicate
    - submit: submit_order
    - reconciler: reconcile
    - alpaca_adapter: AlpacaAdapter (selected only when LIVE_TRADING + keys present)

Executor selection (INTERFACES.md §9, §10b.2, spec §4.1):
    Default executor_backend=sim → SimExecutor.
    executor_backend=alpaca_paper + keys present → AlpacaAdapter (paper endpoint only).
"""
from __future__ import annotations

from arbiter.execution.idempotency import dedup_hash, ensure_not_duplicate
from arbiter.execution.submit import submit_order, SubmitResult
from arbiter.execution.reconciler import reconcile
from arbiter.execution.alpaca_adapter import build_executor

__all__ = [
    "dedup_hash",
    "ensure_not_duplicate",
    "submit_order",
    "SubmitResult",
    "reconcile",
    "build_executor",
]
