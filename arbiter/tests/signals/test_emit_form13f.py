# tests/signals/test_emit_form13f.py
from datetime import datetime, timezone
from arbiter.signals.detection import Signal, SignalType
from arbiter.signals.emit import emit_opinion

NOW = datetime(2026, 6, 23, tzinfo=timezone.utc)

def _sig(ticker, txn, conv=0.6):
    return Signal(signal_type=SignalType.FUND_HOLDING, ticker=ticker, source="form13f",
                  person_ids=("p1",), filing_ids=("f1",), window_start=NOW, window_end=NOW,
                  conviction_score=conv, meta={"txn_type": txn}, as_of=NOW)

def test_emit_fund_long():
    op = emit_opinion(_sig("NVDA", "P"), NOW)
    assert op is not None
    assert op.advisor_id == "A1.fund"
    assert op.horizon_days == 180
    assert op.stance_score > 0

def test_emit_fund_exit_is_bearish():
    op = emit_opinion(_sig("TSLA", "S"), NOW)
    assert op is not None and op.advisor_id == "A1.fund"
    assert op.stance_score < 0
