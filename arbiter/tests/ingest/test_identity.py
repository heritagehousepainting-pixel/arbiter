"""Tests for arbiter.ingest.identity.resolver (Lane 5c).

Covers:
  - Same person across two filings → single person_id (dedup).
  - Name normalisation: whitespace, case, trailing punctuation, suffixes.
  - Different sources → different person_ids for the same raw name.
  - Hinted person_id is honoured on first insert and stable on lookup.
  - Insert-only: no rows are ever deleted or updated.
"""
from __future__ import annotations

import sqlite3

import pytest

from arbiter.db.connection import get_connection
from arbiter.db.migrate import run_migrations
from arbiter.ingest.identity.resolver import resolve_person, _normalize_name


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def conn(tmp_path) -> sqlite3.Connection:
    """Migrated SQLite connection with identity schema applied."""
    db = str(tmp_path / "identity_test.db")
    c = get_connection(db)
    run_migrations(c)
    return c


def _clock() -> str:
    return "2026-06-19T00:00:00+00:00"


# ---------------------------------------------------------------------------
# Name normalisation unit tests
# ---------------------------------------------------------------------------

def test_normalize_strips_whitespace() -> None:
    assert _normalize_name("  Nancy Pelosi  ") == "NANCY PELOSI"


def test_normalize_collapses_internal_spaces() -> None:
    assert _normalize_name("Tim  Cook") == "TIM COOK"


def test_normalize_uppercases() -> None:
    assert _normalize_name("elon musk") == "ELON MUSK"


def test_normalize_strips_trailing_period() -> None:
    # Middle-initial period ("E.") is also stripped, so result has no "E."
    assert _normalize_name("Warren E. Buffett.") == "WARREN E BUFFETT"


def test_normalize_strips_trailing_comma() -> None:
    assert _normalize_name("Gates, Bill,") == "GATES, BILL"


def test_normalize_strips_jr_suffix() -> None:
    assert _normalize_name("John Smith Jr.") == "JOHN SMITH"


def test_normalize_strips_sr_suffix() -> None:
    assert _normalize_name("Robert Jones Sr") == "ROBERT JONES"


def test_normalize_strips_ii_suffix() -> None:
    assert _normalize_name("Richard Roe II") == "RICHARD ROE"


def test_normalize_strips_iii_suffix() -> None:
    assert _normalize_name("William Henry III") == "WILLIAM HENRY"


def test_normalize_preserves_midname_roman() -> None:
    """Roman numeral in the middle of the name is not stripped."""
    assert _normalize_name("Henry III Smith") == "HENRY III SMITH"


# ---------------------------------------------------------------------------
# resolve_person deduplication
# ---------------------------------------------------------------------------

def test_same_person_same_id(conn) -> None:
    """Two calls with the same name+source return the same person_id."""
    id1 = resolve_person("Nancy Pelosi", "congress", None, conn, _clock)
    id2 = resolve_person("Nancy Pelosi", "congress", None, conn, _clock)
    assert id1 == id2


def test_same_person_different_name_variants(conn) -> None:
    """Variant spellings that normalise to the same string map to the same id."""
    id1 = resolve_person("Tim Cook", "form4", None, conn, _clock)
    id2 = resolve_person("tim cook", "form4", None, conn, _clock)
    id3 = resolve_person("TIM COOK", "form4", None, conn, _clock)
    assert id1 == id2 == id3


def test_jr_suffix_dedup(conn) -> None:
    """'John Smith Jr.' and 'John Smith' resolve to the same person."""
    id1 = resolve_person("John Smith Jr.", "form4", None, conn, _clock)
    id2 = resolve_person("John Smith", "form4", None, conn, _clock)
    assert id1 == id2


def test_different_sources_different_ids(conn) -> None:
    """The same raw name in different sources gets different person_ids."""
    id_form4 = resolve_person("Nancy Pelosi", "form4", None, conn, _clock)
    id_congress = resolve_person("Nancy Pelosi", "congress", None, conn, _clock)
    assert id_form4 != id_congress


def test_different_names_different_ids(conn) -> None:
    """Two distinct people have different person_ids."""
    id1 = resolve_person("Elon Musk", "form4", None, conn, _clock)
    id2 = resolve_person("Tim Cook", "form4", None, conn, _clock)
    assert id1 != id2


def test_returns_ulid_string(conn) -> None:
    """resolve_person returns a 26-char Crockford base32 ULID."""
    import re
    ulid_re = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
    pid = resolve_person("Satya Nadella", "form4", None, conn, _clock)
    assert ulid_re.match(pid), f"Not a ULID: {pid!r}"


def test_insert_only_no_extra_rows(conn) -> None:
    """Repeated resolve for the same person does not insert duplicate rows."""
    resolve_person("Jensen Huang", "form4", None, conn, _clock)
    resolve_person("Jensen Huang", "form4", None, conn, _clock)
    resolve_person("jensen huang", "form4", None, conn, _clock)
    count = conn.execute("SELECT count(*) FROM people").fetchone()[0]
    assert count == 1


def test_two_filings_one_person_id(conn) -> None:
    """Simulate two separate filings arriving for the same insider — one person_id."""
    # Filing 1 from EDGAR
    pid_a = resolve_person("Elon R. Musk", "form4", None, conn, _clock)
    # Filing 2 from EDGAR (different raw name casing/punctuation)
    pid_b = resolve_person("ELON R MUSK", "form4", None, conn, _clock)
    assert pid_a == pid_b, "Both filings must resolve to the same person_id"


def test_hint_person_id_used_on_first_insert(conn) -> None:
    """If hints['person_id'] is supplied it becomes the stored ULID."""
    from arbiter.db.helpers import generate_ulid
    hint_id = generate_ulid()
    pid = resolve_person(
        "Warren Buffett",
        "congress",
        {"person_id": hint_id},
        conn,
        _clock,
    )
    assert pid == hint_id


def test_hint_person_id_stable_on_lookup(conn) -> None:
    """Subsequent calls without the hint still return the hinted ULID."""
    from arbiter.db.helpers import generate_ulid
    hint_id = generate_ulid()
    resolve_person("Warren Buffett", "congress", {"person_id": hint_id}, conn, _clock)
    pid2 = resolve_person("Warren Buffett", "congress", None, conn, _clock)
    assert pid2 == hint_id
