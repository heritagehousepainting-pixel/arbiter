from datetime import datetime, timezone

from arbiter.refresh.digest import build_digest, push_digest
from arbiter.refresh.types import (
    RefreshReport, PositionFinding, MacroResult, MacroFinding,
    HealthResult, StaleSource, Severity,
)


def _report():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    return RefreshReport(
        as_of=now,
        positions=[PositionFinding("UBER", ["DOJ probe"], -0.6, Severity.HIGH, True)],
        macro=MacroResult([MacroFinding("CPI Thu", Severity.HIGH, ["UBER"],
                                        ["reuters.com"])], [], True, ""),
        health=HealthResult([StaleSource("form13f", "200d ago", True)]),
        fed_tickers=["UBER"], reingested=["form13f"])


def test_build_digest_has_sections():
    md = build_digest(_report())
    assert "MONDAY REFRESH" in md.upper()
    assert "UBER" in md and "CPI Thu" in md and "form13f" in md


def test_push_digest_calls_notify():
    calls = {}
    class _A:
        def notify(self, title, body, *, as_of):
            calls.update(title=title, body=body)
    push_digest(_report(), alerting=_A())
    assert "Monday Refresh" in calls["title"]
