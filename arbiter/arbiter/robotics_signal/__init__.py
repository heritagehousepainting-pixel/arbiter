"""Robotics early-insight signal (#3) — twice-weekly web-search scan of the
robotics universe → broad developments + trigger-hit flagging → phone digest.

Cloned from ``arbiter/arbiter/refresh/`` (Monday Refresh). This package (part 1)
is the self-contained scanner core: scan -> orchestrator -> digest, hermetic under
``FakeLLM``. Config/CLI/daemon wiring and the probationary advisor land later.
See docs/specs/2026-07-13-robotics-signal-design.md.
"""
