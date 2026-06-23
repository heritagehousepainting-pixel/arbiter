"""Tests for arbiter.safety.gate — is_trading_allowed (Lane L4).

Covers INTERFACES.md §8 and spec §3.9:
    - 0/1/2 advisor cases produce HALTED/DEGRADED/NORMAL decisions
    - Tripped breaker forces allowed=False regardless of quorum
    - breaker_provider raising → fail-closed (not allowed)
    - Audit log is written for every decision
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from arbiter.contract.seams import TradingDecision
from arbiter.db.audit import read_audit
from arbiter.safety.gate import is_trading_allowed
from arbiter.types import DegradationLevel


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

class _FakeAccount:
    """Minimal stand-in for an account object."""
    def __repr__(self) -> str:
        return "<FakeAccount>"


def _no_breakers() -> list[str]:
    """Provider that reports no tripped breakers."""
    return []


def _tripped(name: str):
    """Factory: provider that reports one named breaker tripped."""
    def _provider() -> list[str]:
        return [name]
    return _provider


def _raising_provider() -> list[str]:
    """Provider that raises an exception (simulates unreachable breaker svc)."""
    raise RuntimeError("breaker service unreachable")


@pytest.fixture()
def account() -> _FakeAccount:
    return _FakeAccount()


@pytest.fixture()
def audit_path(tmp_path: Path) -> Path:
    return tmp_path / "audit.jsonl"


# ---------------------------------------------------------------------------
# Helper that calls gate with a custom audit_path so tests don't collide.
# We patch the audit module's path resolver via the audit_path param of audit().
# The gate calls audit() without an explicit audit_path, so we need to redirect
# it via the config.  Easiest: we override the audit module-level _clock and
# pass audit_path directly via monkeypatching.
# ---------------------------------------------------------------------------

def _gate(
    account: object,
    live_advisor_count: int,
    breaker_provider=None,
    audit_path: Path | None = None,
    monkeypatch=None,
) -> TradingDecision:
    """Call is_trading_allowed, optionally redirecting audit to tmp_path."""
    if audit_path is not None and monkeypatch is not None:
        import arbiter.db.audit as _audit_mod
        import arbiter.safety.gate as _gate_mod

        # Patch the audit function the gate uses to write to tmp_path.
        original_audit = _audit_mod.audit

        def _patched_audit(event: str, payload: dict, **kwargs):
            kwargs.setdefault("audit_path", audit_path)
            original_audit(event, payload, **kwargs)

        monkeypatch.setattr(_gate_mod, "audit", _patched_audit)

    return is_trading_allowed(
        account,
        live_advisor_count=live_advisor_count,
        breaker_provider=breaker_provider,
    )


# ---------------------------------------------------------------------------
# Quorum → HALTED (0 advisors)
# ---------------------------------------------------------------------------

class TestGateZeroAdvisors:
    """0 live advisors → HALTED, allowed=False, multiplier=0.0."""

    def test_not_allowed(self, account: _FakeAccount, tmp_path: Path) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=0,
            breaker_provider=_no_breakers,
        )
        assert decision.allowed is False

    def test_level_halted(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=0,
            breaker_provider=_no_breakers,
        )
        assert decision.level == DegradationLevel.HALTED

    def test_multiplier_zero(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=0,
            breaker_provider=_no_breakers,
        )
        assert decision.size_multiplier == pytest.approx(0.0)

    def test_reasons_non_empty(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=0,
            breaker_provider=_no_breakers,
        )
        assert len(decision.reasons) >= 1


# ---------------------------------------------------------------------------
# Quorum → DEGRADED (1 advisor)
# ---------------------------------------------------------------------------

class TestGateOneAdvisor:
    """1 live advisor → DEGRADED, allowed=True, multiplier=0.25."""

    def test_allowed(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=1,
            breaker_provider=_no_breakers,
        )
        assert decision.allowed is True

    def test_level_degraded(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=1,
            breaker_provider=_no_breakers,
        )
        assert decision.level == DegradationLevel.DEGRADED

    def test_multiplier_quarter(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=1,
            breaker_provider=_no_breakers,
        )
        assert decision.size_multiplier == pytest.approx(0.25)

    def test_reasons_non_empty(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=1,
            breaker_provider=_no_breakers,
        )
        assert len(decision.reasons) >= 1


# ---------------------------------------------------------------------------
# Quorum → NORMAL (2+ advisors)
# ---------------------------------------------------------------------------

class TestGateTwoPlusAdvisors:
    """2+ live advisors → NORMAL, allowed=True, multiplier=1.0."""

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_allowed(self, account: _FakeAccount, n: int) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=n,
            breaker_provider=_no_breakers,
        )
        assert decision.allowed is True

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_level_normal(self, account: _FakeAccount, n: int) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=n,
            breaker_provider=_no_breakers,
        )
        assert decision.level == DegradationLevel.NORMAL

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_multiplier_one(self, account: _FakeAccount, n: int) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=n,
            breaker_provider=_no_breakers,
        )
        assert decision.size_multiplier == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Tripped breaker overrides quorum
# ---------------------------------------------------------------------------

class TestGateTrippedBreaker:
    """Any tripped breaker forces allowed=False regardless of quorum."""

    def test_breaker_blocks_two_advisors(self, account: _FakeAccount) -> None:
        """Full quorum + tripped breaker → still not allowed."""
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_tripped("daily_loss_2pct"),
        )
        assert decision.allowed is False

    def test_breaker_blocks_one_advisor(self, account: _FakeAccount) -> None:
        """1 advisor + tripped breaker → not allowed."""
        decision = is_trading_allowed(
            account,
            live_advisor_count=1,
            breaker_provider=_tripped("mirofish_3x_fail"),
        )
        assert decision.allowed is False

    def test_breaker_halts_multiplier(self, account: _FakeAccount) -> None:
        """Tripped breaker forces size_multiplier=0.0."""
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_tripped("broker_non_200"),
        )
        assert decision.size_multiplier == pytest.approx(0.0)

    def test_breaker_name_in_reasons(self, account: _FakeAccount) -> None:
        """Reason list must name the tripped breaker."""
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_tripped("a3_vol_anomaly"),
        )
        combined = " ".join(decision.reasons)
        assert "a3_vol_anomaly" in combined

    def test_multiple_breakers_all_named(self, account: _FakeAccount) -> None:
        """All tripped breaker names appear in reasons."""
        def _multi() -> list[str]:
            return ["daily_loss_2pct", "broker_non_200"]

        decision = is_trading_allowed(
            account,
            live_advisor_count=3,
            breaker_provider=_multi,
        )
        assert decision.allowed is False
        combined = " ".join(decision.reasons)
        assert "daily_loss_2pct" in combined
        assert "broker_non_200" in combined

    def test_level_halted_when_breaker_tripped(self, account: _FakeAccount) -> None:
        """Tripped breaker raises level to HALTED."""
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_tripped("confidence_shift_30pct"),
        )
        assert decision.level == DegradationLevel.HALTED


# ---------------------------------------------------------------------------
# Fail-closed: breaker_provider raises
# ---------------------------------------------------------------------------

class TestGateFailClosed:
    """If breaker_provider raises, gate is fail-closed → not allowed."""

    def test_not_allowed_when_provider_raises(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_raising_provider,
        )
        assert decision.allowed is False

    def test_multiplier_zero_when_provider_raises(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_raising_provider,
        )
        assert decision.size_multiplier == pytest.approx(0.0)

    def test_level_halted_when_provider_raises(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_raising_provider,
        )
        assert decision.level == DegradationLevel.HALTED

    def test_reasons_mention_exception(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_raising_provider,
        )
        combined = " ".join(decision.reasons).lower()
        # Must mention the fail-closed outcome in some way
        assert "fail" in combined or "exception" in combined or "raised" in combined

    def test_fail_closed_even_with_zero_advisors(self, account: _FakeAccount) -> None:
        """Fail-closed applies regardless of quorum state."""
        decision = is_trading_allowed(
            account,
            live_advisor_count=0,
            breaker_provider=_raising_provider,
        )
        assert decision.allowed is False
        assert decision.size_multiplier == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# No breaker_provider (None)
# ---------------------------------------------------------------------------

class TestGateNoBreakerProvider:
    """When breaker_provider=None, breaker checks are skipped (no fault)."""

    def test_two_advisors_allowed_without_provider(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=None,
        )
        assert decision.allowed is True
        assert decision.size_multiplier == pytest.approx(1.0)

    def test_zero_advisors_halted_without_provider(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=0,
            breaker_provider=None,
        )
        assert decision.allowed is False
        assert decision.level == DegradationLevel.HALTED


# ---------------------------------------------------------------------------
# Return type contract
# ---------------------------------------------------------------------------

class TestGateReturnType:
    """is_trading_allowed always returns a frozen TradingDecision."""

    def test_returns_trading_decision(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_no_breakers,
        )
        assert isinstance(decision, TradingDecision)

    def test_decision_frozen(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_no_breakers,
        )
        with pytest.raises((AttributeError, TypeError)):
            decision.allowed = False  # type: ignore[misc]

    def test_reasons_is_list(self, account: _FakeAccount) -> None:
        decision = is_trading_allowed(
            account,
            live_advisor_count=2,
            breaker_provider=_no_breakers,
        )
        assert isinstance(decision.reasons, list)


# ---------------------------------------------------------------------------
# Audit: every decision is written to the audit log
# ---------------------------------------------------------------------------

class TestGateAudit:
    """is_trading_allowed audits every call."""

    def test_audit_written_on_allowed(
        self, account: _FakeAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit_file = tmp_path / "audit.jsonl"
        _gate(account, 2, _no_breakers, audit_path=audit_file, monkeypatch=monkeypatch)
        records = read_audit(audit_path=audit_file)
        assert len(records) == 1
        assert records[0]["event"] == "safety_gate_decision"

    def test_audit_written_on_halted(
        self, account: _FakeAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit_file = tmp_path / "audit.jsonl"
        _gate(account, 0, _no_breakers, audit_path=audit_file, monkeypatch=monkeypatch)
        records = read_audit(audit_path=audit_file)
        assert len(records) == 1
        payload = records[0]["payload"]
        assert payload["allowed"] is False
        assert payload["level"] == "HALTED"

    def test_audit_written_on_breaker_raise(
        self, account: _FakeAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit_file = tmp_path / "audit.jsonl"
        _gate(
            account,
            2,
            _raising_provider,
            audit_path=audit_file,
            monkeypatch=monkeypatch,
        )
        records = read_audit(audit_path=audit_file)
        assert len(records) == 1
        payload = records[0]["payload"]
        assert payload["breaker_error"] is True

    def test_audit_payload_has_advisor_count(
        self, account: _FakeAccount, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        audit_file = tmp_path / "audit.jsonl"
        _gate(account, 3, _no_breakers, audit_path=audit_file, monkeypatch=monkeypatch)
        records = read_audit(audit_path=audit_file)
        assert records[0]["payload"]["live_advisor_count"] == 3
