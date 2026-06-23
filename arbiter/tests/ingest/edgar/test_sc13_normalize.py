"""Schedule 13D/13G normalize tests (offline)."""
from __future__ import annotations

import json

from arbiter.ingest.edgar.sc13_normalize import normalize_sc13


def _row(**overrides) -> dict:
    base = {
        "ticker": "AAPL",
        "person_id": "0007777777",
        "person_name": "Activist Capital LP",
        "filing_ts": "2026-03-15T00:00:00+00:00",
        "schedule": "13D",
        "is_amendment": False,
        "is_activist": True,
        "percent_of_class": 8.5,
        "aggregate_amount": 1300000.0,
        "cusip": "037833100",
        "transaction_code": "P",
        "txn_idx": 0,
        "accession": "0009999999-26-000008",
        "is_10b5_1": False,
    }
    base.update(overrides)
    return base


def test_normalize_sc13d_maps_to_rawfiling():
    out = normalize_sc13([_row()])
    assert len(out) == 1
    f = out[0]
    assert f["source"] == "form13d"
    assert f["txn_type"] == "P"
    assert f["amount_low"] is None
    assert f["amount_high"] is None
    assert f["price"] is None
    assert f["is_10b5_1"] is False
    assert f["shares"] == 1300000.0
    assert f["ticker"] == "AAPL"
    assert f["person_id"] == "0007777777"

    raw = json.loads(f["raw_json"])
    assert raw["schedule"] == "13D"
    assert raw["percent_of_class"] == 8.5
    assert raw["is_activist"] is True
    assert raw["cusip"] == "037833100"


def test_normalize_drops_subthreshold_non_amendment():
    out = normalize_sc13([_row(percent_of_class=4.0, is_amendment=False)])
    assert out == []


def test_normalize_keeps_subthreshold_amendment_as_exit():
    out = normalize_sc13(
        [_row(percent_of_class=4.1, is_amendment=True, transaction_code="S")]
    )
    assert len(out) == 1
    assert out[0]["is_amendment"] is True
    assert out[0]["txn_type"] == "S"


def test_normalize_none_aggregate_yields_zero_shares():
    out = normalize_sc13([_row(aggregate_amount=None)])
    assert len(out) == 1
    assert out[0]["shares"] == 0.0


def test_normalize_13g_passive():
    out = normalize_sc13(
        [_row(schedule="13G", is_activist=False, percent_of_class=6.2)]
    )
    assert len(out) == 1
    assert json.loads(out[0]["raw_json"])["is_activist"] is False
