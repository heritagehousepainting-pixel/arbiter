"""Tests for arbiter.signals.opinion_store — sub-project #5a (real attribution).

OFFLINE: in-memory SQLite, injected as_of, no network, no datetime.now().
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

import pytest

from arbiter.contract.opinion import Opinion
from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.signals import opinion_store
from arbiter.types import ConfidenceSource

UTC = timezone.utc
T = datetime(2025, 6, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = get_connection(":memory:")
    run_migrations(c)
    return c


def _op(
    *,
    advisor_id: str = "A1.insider",
    ticker: str = "AAPL",
    stance: float = 0.7,
    confidence: float = 0.8,
    horizon_days: int = 180,
    fingerprint: str = "fp-1",
) -> Opinion:
    return Opinion(
        advisor_id=advisor_id,
        ticker=ticker,
        stance_score=stance,
        confidence=confidence,
        confidence_source=ConfidenceSource.MODELED,
        horizon_days=horizon_days,
        as_of=T,
        rationale="test",
        source_fingerprint=fingerprint,
        run_group_id="rg-1",
    )


class TestPersist:
    def test_persists_with_idea_id_and_fields(self, conn):
        pk = opinion_store.persist_opinion(conn, _op(), idea_id="IDEA-1", as_of=T)
        assert isinstance(pk, str)
        rows = opinion_store.query_opinions_for_idea(conn, "IDEA-1")
        assert len(rows) == 1
        row = rows[0]
        assert row["advisor_id"] == "A1.insider"
        assert row["idea_id"] == "IDEA-1"
        assert row["stance_score"] == pytest.approx(0.7)
        assert row["confidence"] == pytest.approx(0.8)
        # PIT-clean: created_at == decision as_of.
        assert row["created_at"] == T.isoformat()
        assert row["as_of"] == T.isoformat()

    def test_null_idea_id_persisted_for_audit(self, conn):
        opinion_store.persist_opinion(conn, _op(), idea_id=None, as_of=T)
        # Not linked to any idea, but recoverable via the generic query.
        rows = opinion_store.query_opinions(conn, advisor_id="A1.insider")
        assert len(rows) == 1
        assert rows[0]["idea_id"] is None
        assert opinion_store.query_opinions_for_idea(conn, "IDEA-1") == []

    def test_idempotent_same_as_of(self, conn):
        pk1 = opinion_store.persist_opinion(conn, _op(), idea_id="IDEA-1", as_of=T)
        pk2 = opinion_store.persist_opinion(conn, _op(), idea_id="IDEA-1", as_of=T)
        assert pk1 == pk2  # guard returned the existing row
        assert len(opinion_store.query_opinions_for_idea(conn, "IDEA-1")) == 1

    def test_idempotent_null_idea_id(self, conn):
        opinion_store.persist_opinion(conn, _op(), idea_id=None, as_of=T)
        opinion_store.persist_opinion(conn, _op(), idea_id=None, as_of=T)
        assert len(opinion_store.query_opinions(conn, advisor_id="A1.insider")) == 1

    def test_distinct_advisors_both_linked(self, conn):
        opinion_store.persist_opinion(
            conn, _op(advisor_id="A1.insider", fingerprint="fp-a"),
            idea_id="IDEA-1", as_of=T,
        )
        opinion_store.persist_opinion(
            conn, _op(advisor_id="A1.congress", fingerprint="fp-b"),
            idea_id="IDEA-1", as_of=T,
        )
        rows = opinion_store.query_opinions_for_idea(conn, "IDEA-1")
        assert {r["advisor_id"] for r in rows} == {"A1.insider", "A1.congress"}
