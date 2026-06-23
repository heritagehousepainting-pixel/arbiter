"""Book-state exposure accumulator — W-RISKBOOK.

The engine's ``_bound_decide`` currently calls ``decide()`` with no book
state, so ``max_open_positions`` / ``max_gross_pct`` / ``max_sector_pct``
evaluate against an empty book and never bind.  This module provides a pure,
immutable ``RiskBook`` the engine uses to track running exposure across a
decision cycle and feed those caps with real numbers.

CRITICAL UNITS
--------------
All exposure here is **notional USD market value**.  In this path a
``PaperOrder.qty`` is a *dollar amount*, not a share count, and held
positions are passed in as ``{ticker: usd_market_value}`` (the engine
computes these from broker positions × price).  Every value tracked by this
book is therefore USD.

The values are shaped to feed ``decide()`` exactly::

    decide(
        current_open_positions=book.open_positions(),
        current_gross_exposure=book.gross_exposure(),
        current_sector_exposure=book.sector_exposure_for(ticker),
    )

Note ``decide()`` takes ``current_sector_exposure`` as a single *float*
(the notional of the sector the ticker belongs to), not a dict — see
``sector_exposure_for`` / ``as_decide_kwargs``.

Purity
------
No I/O, no network, no ``datetime.now()``.  ``add()`` returns a brand-new
``RiskBook`` and never mutates the receiver.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from types import MappingProxyType

__all__ = ["RiskBook"]


class RiskBook:
    """Immutable running-exposure accumulator (all values notional USD).

    Parameters
    ----------
    held:
        Starting positions as ``{ticker: usd_market_value}``.  The engine
        computes these from broker positions × price before a cycle.
    sector_for:
        ``Callable[[str], str]`` mapping a ticker to its sector name.
        Injected so the book has no knowledge of the sector taxonomy.
    """

    __slots__ = ("_held", "_sector_for")

    def __init__(
        self,
        held: Mapping[str, float],
        sector_for: Callable[[str], str],
    ) -> None:
        # Defensive copy → genuine immutability of the per-ticker notional map.
        self._held: Mapping[str, float] = MappingProxyType(dict(held))
        self._sector_for = sector_for

    # ------------------------------------------------------------------
    # Readers — shaped to feed decide()
    # ------------------------------------------------------------------

    def open_positions(self) -> int:
        """Number of distinct held positions → ``current_open_positions``."""
        return len(self._held)

    def gross_exposure(self) -> float:
        """Total notional USD across all positions → ``current_gross_exposure``."""
        return float(sum(self._held.values()))

    def sector_exposure(self) -> dict[str, float]:
        """Notional USD grouped by sector name (``{sector: usd}``)."""
        out: dict[str, float] = {}
        for ticker, notional in self._held.items():
            sector = self._sector_for(ticker)
            out[sector] = out.get(sector, 0.0) + float(notional)
        return out

    def sector_exposure_for(self, ticker: str) -> float:
        """Notional USD of the sector ``ticker`` belongs to.

        This is what ``decide(current_sector_exposure=...)`` expects: the
        already-committed notional in the *ticker's* sector.  Returns 0.0
        when nothing is held in that sector.
        """
        sector = self._sector_for(ticker)
        return self.sector_exposure().get(sector, 0.0)

    def as_decide_kwargs(self, ticker: str) -> dict[str, object]:
        """Exact kwargs for ``decide()`` book-state params for ``ticker``."""
        return {
            "current_open_positions": self.open_positions(),
            "current_gross_exposure": self.gross_exposure(),
            "current_sector_exposure": self.sector_exposure_for(ticker),
        }

    # ------------------------------------------------------------------
    # Writer — fold in a new order (returns a NEW book; never mutates)
    # ------------------------------------------------------------------

    def add(self, ticker: str, notional_usd: float) -> "RiskBook":
        """Return a NEW book with ``notional_usd`` folded into ``ticker``.

        Call this AFTER a successful submit so subsequent ``decide()`` calls
        in the same cycle see the freshly-committed exposure.  Adding to a
        ticker already in the book grows its notional (does not create a new
        position); adding a new ticker increments the open-position count.
        """
        new_held = dict(self._held)
        new_held[ticker] = new_held.get(ticker, 0.0) + float(notional_usd)
        return RiskBook(held=new_held, sector_for=self._sector_for)
