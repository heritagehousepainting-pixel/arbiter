"""Arbiter database package.

Lane 1 provides only the connection factory (``get_connection``).
Lane 2 owns the schema, migrations, helpers, and audit writer.
"""
from __future__ import annotations

from arbiter.db.connection import get_connection

__all__ = ["get_connection"]
