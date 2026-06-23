"""MiroFish invocation triage — Lane 13.

Implements the invoke/skip decision for the MiroFish advisor per the spec:

    INVOKE when:
        - bucket is SWING (maps to SHORT) or LONG
        - bucket is DAY (maps to INTRADAY-adjacent? — see note) only if
          >30 minutes remain until entry

    SKIP when:
        - bucket is INTRADAY or any horizon < 5 minutes
        - bucket is driven by NEWS signal (signal_kind == "NEWS")
        - any other case that doesn't match an invoke rule

Design note on "SWING/LONG" vs bucket names:
    The spec uses informal names (SWING, LONG, DAY, INTRADAY). Mapping to
    HorizonBucket enum values:
        SWING  → SHORT   (1–30 days; swing-trading window)
        LONG   → LONG    (121–365 days)
        DAY    → INTRADAY (<1 day, intraday)
        < 5min → treat as INTRADAY, always skip

    Per the spec:
        - always invoke on SWING (SHORT) and LONG
        - invoke on DAY (INTRADAY) ONLY if minutes_to_entry > 30
        - never invoke on INTRADAY with < 5 min remaining
        - never invoke on NEWS-tagged signals

MiroFish itself is injected (never imported directly — AGPL restriction per
INTERFACES.md §11.5).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

from arbiter.types import HorizonBucket


# ---------------------------------------------------------------------------
# Triage result
# ---------------------------------------------------------------------------

class TriageAction(str):
    """String sentinel for the triage decision."""
    INVOKE = "invoke"
    SKIP   = "skip"


@dataclass(frozen=True)
class TriageResult:
    """Result of the MiroFish triage check.

    Attributes
    ----------
    action:
        ``"invoke"`` or ``"skip"``.
    reason:
        Human-readable explanation for logging/audit.
    """
    action: str
    reason: str

    @property
    def should_invoke(self) -> bool:
        """True if MiroFish should be called this cycle."""
        return self.action == TriageAction.INVOKE


# ---------------------------------------------------------------------------
# Triage callable protocol (for type checking injected mirofish)
# ---------------------------------------------------------------------------

class MiroFishCallable(Protocol):
    """Protocol for the injected MiroFish callable.

    MiroFish is injected — never imported (AGPL restriction).
    The callable receives a ticker and returns a list of Opinions (or raises).
    """
    def __call__(self, ticker: str, **kwargs: Any) -> list[Any]: ...


# ---------------------------------------------------------------------------
# Core triage logic
# ---------------------------------------------------------------------------

def triage_mirofish(
    bucket: HorizonBucket,
    *,
    minutes_to_entry: float | None = None,
    signal_kind: str | None = None,
) -> TriageResult:
    """Decide whether to invoke MiroFish this cycle for *bucket*.

    Parameters
    ----------
    bucket:
        The HorizonBucket being evaluated.
    minutes_to_entry:
        For INTRADAY (DAY) trades only: minutes remaining until estimated
        entry.  ``None`` is treated as unknown → skip for INTRADAY.
    signal_kind:
        Optional tag on the triggering signal.  ``"NEWS"`` always skips
        MiroFish regardless of bucket.

    Returns
    -------
    TriageResult
        ``.action`` is ``"invoke"`` or ``"skip"``.
        ``.reason`` explains the decision.
    """
    # Rule 1: NEWS signals → always skip
    if signal_kind is not None and signal_kind.upper() == "NEWS":
        return TriageResult(
            action=TriageAction.SKIP,
            reason="Signal kind is NEWS — MiroFish never invoked on news signals",
        )

    # Rule 2: SWING (SHORT) → always invoke
    if bucket is HorizonBucket.SHORT:
        return TriageResult(
            action=TriageAction.INVOKE,
            reason="SWING (SHORT bucket) — always invoke MiroFish",
        )

    # Rule 3: LONG → always invoke
    if bucket is HorizonBucket.LONG:
        return TriageResult(
            action=TriageAction.INVOKE,
            reason="LONG bucket — always invoke MiroFish",
        )

    # Rule 4: MEDIUM → skip (not mentioned in invoke rules)
    if bucket is HorizonBucket.MEDIUM:
        return TriageResult(
            action=TriageAction.SKIP,
            reason="MEDIUM bucket — not in MiroFish invoke criteria",
        )

    # Rule 5: INTRADAY (DAY) — conditional on minutes_to_entry
    if bucket is HorizonBucket.INTRADAY:
        # < 5 min is explicitly "never INTRADAY/<5min NEWS" — always skip
        if minutes_to_entry is not None and minutes_to_entry < 5:
            return TriageResult(
                action=TriageAction.SKIP,
                reason=f"INTRADAY with {minutes_to_entry:.1f} min to entry "
                       f"(< 5 min threshold) — skip MiroFish",
            )

        # DAY: invoke only if > 30 min to entry
        if minutes_to_entry is not None and minutes_to_entry > 30:
            return TriageResult(
                action=TriageAction.INVOKE,
                reason=f"INTRADAY (DAY) with {minutes_to_entry:.1f} min to entry "
                       f"(> 30 min threshold) — invoke MiroFish",
            )

        # minutes_to_entry unknown or <= 30 → skip
        effective_min = minutes_to_entry if minutes_to_entry is not None else 0.0
        return TriageResult(
            action=TriageAction.SKIP,
            reason=f"INTRADAY (DAY) with {effective_min:.1f} min to entry "
                   f"(≤ 30 min or unknown) — skip MiroFish",
        )

    # Fallback safety (shouldn't be reachable with complete HorizonBucket enum)
    return TriageResult(
        action=TriageAction.SKIP,
        reason=f"Unknown bucket {bucket!r} — defaulting to skip (fail-safe)",
    )


def maybe_invoke_mirofish(
    mirofish: MiroFishCallable,
    ticker: str,
    bucket: HorizonBucket,
    *,
    minutes_to_entry: float | None = None,
    signal_kind: str | None = None,
    **mirofish_kwargs: Any,
) -> tuple[TriageResult, list[Any]]:
    """Triage then optionally call the injected MiroFish callable.

    Parameters
    ----------
    mirofish:
        Injected callable (never imported — AGPL restriction).
    ticker:
        Ticker to query.
    bucket:
        HorizonBucket for the idea being evaluated.
    minutes_to_entry:
        Minutes until expected entry (INTRADAY gate check).
    signal_kind:
        Optional signal tag; "NEWS" → skip.
    **mirofish_kwargs:
        Forwarded to the mirofish callable if invoked.

    Returns
    -------
    tuple[TriageResult, list]
        The triage result and (if invoked) the list of opinions returned by
        MiroFish, or an empty list if skipped.
    """
    result = triage_mirofish(
        bucket,
        minutes_to_entry=minutes_to_entry,
        signal_kind=signal_kind,
    )

    if result.should_invoke:
        opinions = mirofish(ticker, **mirofish_kwargs)
        return result, opinions

    return result, []
