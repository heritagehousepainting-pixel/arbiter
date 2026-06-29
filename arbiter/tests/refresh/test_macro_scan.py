from datetime import datetime, timezone

from arbiter.refresh.llm import FakeLLM
from arbiter.refresh.macro_scan import scan_macro, parse_macro_json
from arbiter.refresh.types import Severity


def _cfg(key="sk-x", model="claude-opus-4-8"):
    from types import SimpleNamespace
    return SimpleNamespace(anthropic_api_key=key, refresh_model=model)


CANNED = """Here is the analysis.
```json
{"market": [{"summary": "CPI print Thursday", "severity": "high",
             "affected_tickers": ["UBER"], "sources": ["reuters.com"]}],
 "stale_sources": [{"source": "activist_filers", "reason": "Icahn wound down",
                    "sources": ["wsj.com"]}]}
```
"""


def test_scan_parses_findings_and_flags():
    res = scan_macro(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc),
                     _cfg(), llm=FakeLLM(CANNED))
    assert res.available is True
    assert res.findings[0].severity == Severity.HIGH
    assert res.findings[0].affected_tickers == ["UBER"]
    assert res.stale_flags[0].source == "activist_filers"


def test_no_key_is_unavailable():
    res = scan_macro(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc),
                     _cfg(key=""), llm=None)
    assert res.available is False and res.findings == [] and res.stale_flags == []


def test_unparseable_is_unavailable_not_raised():
    res = scan_macro(["UBER"], datetime(2026, 6, 29, tzinfo=timezone.utc),
                     _cfg(), llm=FakeLLM("no json here"))
    assert res.available is False and res.findings == []


def test_parse_ignores_unknown_stale_source():
    findings, flags = parse_macro_json(
        '{"market": [], "stale_sources": [{"source": "nonsense", "reason": "x"}]}')
    assert flags == []
