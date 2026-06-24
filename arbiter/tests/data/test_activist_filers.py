"""Tests for the activist 13D-filer roster (the A1.activist universe).

Pure, no network — verifies roster shape/CIK format so a typo'd or duplicate
CIK can't silently no-op the filer-search path.
"""
from __future__ import annotations

import re

from arbiter.data.activist_filers import ACTIVIST_FILERS, activist_ciks


def test_roster_nonempty_and_named():
    names = {a.name for a in ACTIVIST_FILERS}
    assert len(ACTIVIST_FILERS) >= 5
    # A few anchors we explicitly verified live against EDGAR 2026-06-24.
    assert "Carl Icahn" in names
    assert any("Starboard" in a.fund for a in ACTIVIST_FILERS)
    assert any("Elliott" in a.fund for a in ACTIVIST_FILERS)


def test_ciks_are_ten_digit_and_unique():
    for a in ACTIVIST_FILERS:
        assert re.fullmatch(r"\d{10}", a.cik), f"{a.name} cik not 10-digit: {a.cik}"
    ciks = [a.cik for a in ACTIVIST_FILERS]
    assert len(ciks) == len(set(ciks)), "duplicate activist CIK in roster"


def test_activist_ciks_helper_matches_roster():
    assert set(activist_ciks()) == {a.cik for a in ACTIVIST_FILERS}


def test_activist_ciks_disjoint_from_no_padding_issues():
    # Carl Icahn's active 13D filer CIK (verified 2026-06-24).
    by_name = {a.name: a.cik for a in ACTIVIST_FILERS}
    assert by_name["Carl Icahn"] == "0000921669"
