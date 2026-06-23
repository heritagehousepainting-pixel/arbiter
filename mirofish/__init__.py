"""MiroFish A2 brain — a localhost inference service for arbiter's A2 advisor.

This package is reached by arbiter ONLY over loopback HTTP. It must never
`import arbiter` (enforced by tests/test_isolation.py).
"""

__version__ = "0.1.0"
