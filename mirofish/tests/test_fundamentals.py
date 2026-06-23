"""Tests for compute_fundamentals, incl. the load-bearing PIT exclusion test."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from mirofish.data.sector_valuation import SECTOR_MAP
from mirofish.evidence.fundamentals import compute_fundamentals, pit_concept_values


class _FakeClient:
    """SecFactsClient stand-in: returns a canned companyfacts dict."""

    def __init__(self, facts: dict | None):
        self._facts = facts

    def facts_as_of(self, ticker: str, as_of: datetime) -> dict | None:
        return self._facts


def _annualize(items: list[dict]) -> list[dict]:
    """Give any flow fact that omits `start` a ~365-day (annual) period, so the
    annual selector picks it. Facts that specify their own `start` (e.g. a
    90-day quarter) are left untouched."""
    out = []
    for it in items:
        it = dict(it)
        if "end" in it and "start" not in it:
            end = date.fromisoformat(it["end"][:10])
            it["start"] = (end - timedelta(days=365)).isoformat()
        out.append(it)
    return out


def _facts(revenue_facts, *, net_income=None, shares=None,
           gross_profit=None, operating_income=None) -> dict:
    gaap: dict = {}
    if revenue_facts is not None:
        gaap["Revenues"] = {"units": {"USD": _annualize(revenue_facts)}}
    if net_income is not None:
        gaap["NetIncomeLoss"] = {"units": {"USD": _annualize(net_income)}}
    if gross_profit is not None:
        gaap["GrossProfit"] = {"units": {"USD": _annualize(gross_profit)}}
    if operating_income is not None:
        gaap["OperatingIncomeLoss"] = {"units": {"USD": _annualize(operating_income)}}
    if shares is not None:  # point-in-time; no period
        gaap["WeightedAverageNumberOfDilutedSharesOutstanding"] = {
            "units": {"shares": shares}
        }
    return {"facts": {"us-gaap": gaap}}


_AS_OF = datetime(2026, 6, 1, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# LOAD-BEARING PIT EXCLUSION TEST
# --------------------------------------------------------------------------- #
def test_pit_excludes_fact_filed_after_as_of():
    """A revenue fact filed AFTER as_of must be excluded; the pre-as_of one wins."""
    revenue = [
        # filed BEFORE as_of -> eligible. period ends 2026-03-31.
        {"end": "2026-03-31", "val": 1000.0, "filed": "2026-04-20", "form": "10-Q"},
        # filed AFTER as_of -> MUST be excluded even though period is newer.
        {"end": "2026-06-30", "val": 9999.0, "filed": "2026-07-20", "form": "10-Q"},
    ]
    client = _FakeClient(_facts(revenue))
    ff = compute_fundamentals(
        "AAPL", _AS_OF, client=client, last_close=None, sector_map=SECTOR_MAP
    )
    assert ff is not None
    assert ff.revenue_ttm == 1000.0  # the post-as_of 9999 fact was excluded
    assert ff.as_of_latest_filed == "2026-04-20"


def test_uses_annual_not_partial_period():
    """Regression for the TTM bug: a NEWER quarterly (partial) fact must NOT
    override the full-year ANNUAL value for the pe/ps denominators."""
    revenue = [
        # latest ANNUAL (FY) revenue, period ~365d -> the one we must use.
        {"start": "2025-01-01", "end": "2025-12-31", "val": 4000.0,
         "filed": "2026-02-01", "form": "10-K"},
        # NEWER quarterly (3-month) revenue -> must be IGNORED for the denominator.
        {"start": "2026-01-01", "end": "2026-03-31", "val": 1100.0,
         "filed": "2026-04-20", "form": "10-Q"},
    ]
    net_income = [
        {"start": "2025-01-01", "end": "2025-12-31", "val": 1000.0,
         "filed": "2026-02-01", "form": "10-K"},
        {"start": "2026-01-01", "end": "2026-03-31", "val": 300.0,
         "filed": "2026-04-20", "form": "10-Q"},
    ]
    shares = [{"end": "2026-03-31", "val": 100.0, "filed": "2026-04-20"}]
    client = _FakeClient(_facts(revenue, net_income=net_income, shares=shares))
    ff = compute_fundamentals(
        "AAPL", _AS_OF, client=client, last_close=50.0, sector_map=SECTOR_MAP
    )
    assert ff is not None
    assert ff.revenue_ttm == 4000.0      # annual, NOT the 1100 quarter
    assert ff.net_income_ttm == 1000.0   # annual, NOT the 300 quarter
    # market_cap = 50*100 = 5000; pe = 5000/1000 = 5.0 (annual).
    # The bug would have used the 300 quarter -> pe 16.7 (~3x inflated).
    assert ff.pe_ratio == pytest.approx(5.0)


def test_abstains_when_net_income_exceeds_revenue():
    """Internal-consistency guard: impossible figures (NI > revenue, from a
    cross-company tag mismatch) -> abstain on fundamentals (return None)."""
    revenue = [{"end": "2025-12-31", "val": 27_000_000_000, "filed": "2026-02-01"}]
    net_income = [{"end": "2025-12-31", "val": 120_000_000_000, "filed": "2026-02-01"}]
    shares = [{"end": "2025-12-31", "val": 24_000_000_000, "filed": "2026-02-01"}]
    client = _FakeClient(_facts(revenue, net_income=net_income, shares=shares))
    ff = compute_fundamentals(
        "NVDA", _AS_OF, client=client, last_close=200.0, sector_map=SECTOR_MAP
    )
    assert ff is None  # impossible NI>rev -> degrade to technical-only


def test_pit_concept_values_filters_and_orders():
    facts = _facts([
        {"end": "2025-12-31", "val": 500.0, "filed": "2026-01-15"},
        {"end": "2026-03-31", "val": 600.0, "filed": "2026-04-15"},
        {"end": "2026-06-30", "val": 700.0, "filed": "2026-07-15"},  # after as_of
    ])
    rows = pit_concept_values(facts, "Revenues", _AS_OF)
    vals = [r["val"] for r in rows]
    assert vals == [600.0, 500.0]  # post-as_of dropped, newest-period first


# --------------------------------------------------------------------------- #
# Coverage / None paths
# --------------------------------------------------------------------------- #
def test_no_facts_returns_none():
    assert (
        compute_fundamentals(
            "AAPL", _AS_OF, client=_FakeClient(None), last_close=100.0,
            sector_map=SECTOR_MAP,
        )
        is None
    )


def test_missing_revenue_tag_returns_none():
    client = _FakeClient(_facts(None, net_income=[
        {"end": "2026-03-31", "val": 50.0, "filed": "2026-04-20"}
    ]))
    assert (
        compute_fundamentals(
            "AAPL", _AS_OF, client=client, last_close=100.0, sector_map=SECTOR_MAP
        )
        is None
    )


def test_pe_none_when_net_income_non_positive():
    revenue = [{"end": "2026-03-31", "val": 1000.0, "filed": "2026-04-20"}]
    net_income = [{"end": "2026-03-31", "val": -50.0, "filed": "2026-04-20"}]
    shares = [{"end": "2026-03-31", "val": 100.0, "filed": "2026-04-20"}]
    client = _FakeClient(_facts(revenue, net_income=net_income, shares=shares))
    ff = compute_fundamentals(
        "AAPL", _AS_OF, client=client, last_close=10.0, sector_map=SECTOR_MAP
    )
    assert ff is not None
    assert ff.pe_ratio is None  # earnings <= 0 -> no P/E
    assert ff.ps_ratio == pytest.approx(10.0 * 100.0 / 1000.0)  # P/S still set
    assert ff.valuation_z is None  # no pe -> no z


def test_valuation_z_from_vendored_sector_table():
    # AAPL -> Technology baseline (median_pe=28, stdev_pe=12).
    revenue = [{"end": "2026-03-31", "val": 1000.0, "filed": "2026-04-20"}]
    net_income = [{"end": "2026-03-31", "val": 100.0, "filed": "2026-04-20"}]
    shares = [{"end": "2026-03-31", "val": 40.0, "filed": "2026-04-20"}]
    # market_cap = 100 * 40 = 4000; pe = 4000/100 = 40.
    client = _FakeClient(_facts(revenue, net_income=net_income, shares=shares))
    ff = compute_fundamentals(
        "AAPL", _AS_OF, client=client, last_close=100.0, sector_map=SECTOR_MAP
    )
    assert ff is not None
    assert ff.sector == "Technology"
    assert ff.pe_ratio == pytest.approx(40.0)
    # z = (40 - 28) / 12 = 1.0 -> positive (richer than sector)
    assert ff.valuation_z == pytest.approx(1.0)


def test_margins_and_growth():
    revenue = [
        {"end": "2026-03-31", "val": 1200.0, "filed": "2026-04-20"},
        {"end": "2025-03-31", "val": 1000.0, "filed": "2025-04-20"},  # YoY prior
    ]
    gross = [{"end": "2026-03-31", "val": 600.0, "filed": "2026-04-20"}]
    op = [{"end": "2026-03-31", "val": 300.0, "filed": "2026-04-20"}]
    client = _FakeClient(_facts(revenue, gross_profit=gross, operating_income=op))
    ff = compute_fundamentals(
        "AAPL", _AS_OF, client=client, last_close=None, sector_map=SECTOR_MAP
    )
    assert ff is not None
    assert ff.revenue_ttm == 1200.0
    assert ff.revenue_growth_yoy == pytest.approx(1200.0 / 1000.0 - 1.0)
    assert ff.gross_margin == pytest.approx(0.5)
    assert ff.operating_margin == pytest.approx(0.25)


def test_unmapped_ticker_has_no_sector_or_z():
    revenue = [{"end": "2026-03-31", "val": 1000.0, "filed": "2026-04-20"}]
    net_income = [{"end": "2026-03-31", "val": 100.0, "filed": "2026-04-20"}]
    shares = [{"end": "2026-03-31", "val": 40.0, "filed": "2026-04-20"}]
    client = _FakeClient(_facts(revenue, net_income=net_income, shares=shares))
    ff = compute_fundamentals(
        "ZZZZ", _AS_OF, client=client, last_close=100.0, sector_map=SECTOR_MAP
    )
    assert ff is not None
    assert ff.sector is None
    assert ff.valuation_z is None


def test_never_raises_on_malformed_facts():
    client = _FakeClient({"facts": "garbage"})
    assert (
        compute_fundamentals(
            "AAPL", _AS_OF, client=client, last_close=100.0, sector_map=SECTOR_MAP
        )
        is None
    )
