from datetime import datetime, timezone

from arbiter.refresh.types import (
    Severity, PositionFinding, MacroFinding, StaleFlag, MacroResult,
    StaleSource, HealthResult, RefreshReport,
)


def test_types_construct():
    now = datetime(2026, 6, 29, tzinfo=timezone.utc)
    pf = PositionFinding(ticker="UBER", headlines=["x"], sentiment=-0.4,
                         severity=Severity.HIGH, available=True)
    mf = MacroFinding(summary="CPI Thu", severity=Severity.MEDIUM,
                      affected_tickers=["UBER"], sources=["reuters.com"])
    sf = StaleFlag(source="activist_filers", reason="Icahn wound down",
                   sources=["wsj.com"])
    macro = MacroResult(findings=[mf], stale_flags=[sf], available=True, note="")
    ss = StaleSource(source="fund_managers", reason="CIK 13F stale", confirmed=True)
    health = HealthResult(sources=[ss])
    report = RefreshReport(as_of=now, positions=[pf], macro=macro, health=health,
                           fed_tickers=["UBER"], reingested=["fund_managers"])
    assert report.positions[0].ticker == "UBER"
    assert macro.findings[0].affected_tickers == ["UBER"]
    assert health.confirmed_stale() == [ss]


def test_health_confirmed_stale_filters_unconfirmed():
    a = StaleSource(source="a", reason="r", confirmed=True)
    b = StaleSource(source="b", reason="r", confirmed=False)
    assert HealthResult(sources=[a, b]).confirmed_stale() == [a]
