"""Idea lifecycle orchestrator — Lane 13.

Wires advisors → fusion → policy → execution for one cycle.
Phase-1 path is fully implemented; later hooks are stubbed.

Public surface:
    from arbiter.orchestrator.idea import make_idea, dedupe_key_for
    from arbiter.orchestrator.lifecycle import transition, LEGAL_TRANSITIONS
    from arbiter.orchestrator.triage import triage_mirofish
    from arbiter.orchestrator.scheduler import run_advisors_parallel
    from arbiter.orchestrator.outcome_sweep import sweep_outcomes
    from arbiter.orchestrator.revision import evaluate_revision, RevisionAction
    from arbiter.orchestrator.cycle import run_cycle
"""
from __future__ import annotations
