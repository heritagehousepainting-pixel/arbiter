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

    __slots__ = ("_held", "_sector_for", "_option_overlay")

    def __init__(
        self,
        held: Mapping[str, float],
        sector_for: Callable[[str], str],
        option_overlay: Mapping[str, float] | None = None,
    ) -> None:
        # Defensive copy → genuine immutability of the per-ticker notional map.
        self._held: Mapping[str, float] = MappingProxyType(dict(held))
        self._sector_for = sector_for
        # Options are a SEPARATE working book (2026-07-20 two-working-books):
        # their delta-notional guards the PER-NAME cap only (cross-book
        # anti-doubling) and never counts toward equity gross/sector/count.
        self._option_overlay: Mapping[str, float] = MappingProxyType(
            dict(option_overlay or {})
        )

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

    def name_exposure_for(self, ticker: str) -> float:
        """Notional USD already committed to ``ticker`` itself (0.0 if unheld).

        Feeds ``decide(current_name_exposure=...)`` so an add-on to a held
        name sizes against the per-name cap HEADROOM (Tier-2 #5).
        """
        return float(
            self._held.get(ticker, 0.0) + self._option_overlay.get(ticker, 0.0)
        )

    def as_decide_kwargs(self, ticker: str) -> dict[str, object]:
        """Exact kwargs for ``decide()`` book-state params for ``ticker``."""
        return {
            "current_open_positions": self.open_positions(),
            "current_gross_exposure": self.gross_exposure(),
            "current_sector_exposure": self.sector_exposure_for(ticker),
            "current_name_exposure": self.name_exposure_for(ticker),
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
        return RiskBook(
            held=new_held,
            sector_for=self._sector_for,
            option_overlay=self._option_overlay,
        )

    def add_option_delta(
        self,
        ticker: str,
        delta_adjusted_notional: float,
    ) -> "RiskBook":
        """Return a NEW book with an option's delta-adjusted notional folded in.

        This is the OPTIONS EXPRESSION LAYER's bridge into the equity RiskBook.
        Two-working-books (2026-07-20): options are a SEPARATE budget, so the
        delta-notional folds into the OPTION OVERLAY — it guards the PER-NAME
        cap for the underlying (no equity doubling on a name already expressed
        via options) but does NOT count toward equity gross/sector exposure or
        the open-position count.  The options book's own spend is bounded by
        the premium sleeve (``options_sleeve_pct``) in options/sizing.

        Formula (caller computes before passing here)::

            delta_adjusted_notional = |delta| × 100 × underlying_price × contracts_qty

        The key is the underlying ticker (not the OCC symbol).
        """
        new_overlay = dict(self._option_overlay)
        new_overlay[ticker] = new_overlay.get(ticker, 0.0) + float(delta_adjusted_notional)
        return RiskBook(
            held=dict(self._held),
            sector_for=self._sector_for,
            option_overlay=new_overlay,
        )
