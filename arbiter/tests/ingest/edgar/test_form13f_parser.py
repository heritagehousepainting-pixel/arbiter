from pathlib import Path
from arbiter.ingest.edgar.form13f_parser import parse_form13f_infotable

FIX = Path(__file__).parent / "fixtures" / "form13f_infotable_sample.xml"


def test_parses_holdings():
    rows = parse_form13f_infotable(FIX.read_text())
    assert len(rows) == 2
    nv = next(r for r in rows if r["cusip"] == "67066G104")
    assert nv["issuer_name"] == "NVIDIA CORP"
    assert nv["value_usd"] == 1_500_000.0
    assert nv["shares"] == 10_000.0
    assert nv["put_call"] is None
    ap = next(r for r in rows if r["cusip"] == "037833100")
    assert ap["put_call"] == "Call"


def test_malformed_never_raises():
    assert parse_form13f_infotable("") == []
    assert parse_form13f_infotable("<not><closed>") == []
    assert parse_form13f_infotable("<informationTable></informationTable>") == []
