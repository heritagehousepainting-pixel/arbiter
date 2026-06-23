"""Source-diversity gate for the tips layer — Lane 8.

SHADOW / DORMANT in MVP
-----------------------
This gate enforces the core anti-manipulation rule: a single tip source
(no matter how many accounts it reports) cannot alone produce a corroborated
signal.  Abstain (None) is the only output until ≥ 2 *independent* sources
agree on a ticker.

"Independent" means distinct ``source_id`` values on the ``UnverifiedTip``.
Two tips from @FinTwitGuru and @PumpKing on the same platform (same
``source_id``) count as ONE voice, not two — this is the primary defence
against coordinated pumps using multiple accounts on one platform.

Key conventions (INTERFACES.md §11):
- Abstain is ``None``, never a zero-stance Opinion.
- No ``datetime.now()``.

Public surface
--------------
DiversityGate    — evaluates a list of tips and enforces the ≥ 2 source rule.
corroborate()    — convenience function; returns the set of independent
                   source_ids if corroborated, else None.
"""
from __future__ import annotations

from dataclasses import dataclass

from arbiter.tips.source import UnverifiedTip

# Minimum number of *distinct* source_ids required for corroboration.
_MIN_INDEPENDENT_SOURCES: int = 2


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CorroborationResult:
    """Outcome of the diversity gate for a set of tips on one ticker.

    Fields
    ------
    ticker:
        The ticker that was evaluated.
    corroborated:
        True iff ≥ ``_MIN_INDEPENDENT_SOURCES`` distinct source_ids are
        present among the tips.  When False, callers MUST abstain (return None).
    independent_sources:
        Frozenset of distinct ``source_id`` values that contributed.
    tip_count:
        Total number of tips evaluated (includes duplicates / same source).
    """

    ticker: str
    corroborated: bool
    independent_sources: frozenset[str]
    tip_count: int

    @property
    def n_sources(self) -> int:
        """Number of distinct independent sources."""
        return len(self.independent_sources)


# ---------------------------------------------------------------------------
# DiversityGate
# ---------------------------------------------------------------------------

class DiversityGate:
    """Evaluates whether a set of tips meets the minimum-source-diversity rule.

    Usage::

        gate = DiversityGate()
        result = gate.evaluate(ticker="AAPL", tips=[tip1, tip2, tip3])
        if not result.corroborated:
            return None  # abstain — not corroborated

    The gate is stateless; it evaluates only the tips passed to each call.
    Callers are responsible for collecting tips across adapters before calling.
    """

    def evaluate(
        self,
        ticker: str,
        tips: list[UnverifiedTip],
    ) -> CorroborationResult:
        """Evaluate tips for *ticker* against the diversity rule.

        Parameters
        ----------
        ticker:
            The ticker the tips refer to.  Tips whose ``.ticker`` does not
            match are silently excluded (defensive filter).
        tips:
            All gathered ``UnverifiedTip`` objects for this ticker from all
            adapters.

        Returns
        -------
        CorroborationResult
            ``corroborated=True`` iff ≥ 2 distinct ``source_id`` values are
            present among the matching tips.  Callers MUST return ``None`` when
            ``corroborated=False``.
        """
        matching = [t for t in tips if t.ticker == ticker]
        source_ids = frozenset(t.source_id for t in matching)
        corroborated = len(source_ids) >= _MIN_INDEPENDENT_SOURCES

        return CorroborationResult(
            ticker=ticker,
            corroborated=corroborated,
            independent_sources=source_ids,
            tip_count=len(matching),
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def corroborate(
    ticker: str,
    tips: list[UnverifiedTip],
) -> frozenset[str] | None:
    """Return the set of independent source_ids if corroborated, else None.

    This is the recommended call site for code that only needs a go/no-go
    decision without the full ``CorroborationResult``.

    Parameters
    ----------
    ticker:
        Ticker to check.
    tips:
        Tips gathered from all adapters for this ticker.

    Returns
    -------
    frozenset[str] | None
        The set of distinct ``source_id`` values if corroborated, or ``None``
        if abstain (fewer than 2 independent sources).
    """
    gate = DiversityGate()
    result = gate.evaluate(ticker, tips)
    return result.independent_sources if result.corroborated else None
