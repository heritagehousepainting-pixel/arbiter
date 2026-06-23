"""TipSource ABC and UnverifiedTip dataclass — Lane 8 core.

SHADOW / DORMANT in MVP
-----------------------
An ``UnverifiedTip`` is a raw lead — a (ticker, claim, account, ts, url)
tuple scraped or received from an external source (social media, chat,
web scrape, etc.).

A tip is NOT an Opinion and NEVER becomes one on its own.  The tip layer
enforces abstain (None) until the diversity gate confirms ≥ 2 independent
corroborating sources.  Even then, the A3 advisor is shadow/dormant in
Phase-6 and produces no live fusion signal.

Key conventions (INTERFACES.md §11):
- No ``datetime.now()`` — the ``ts`` field on ``UnverifiedTip`` must be an
  externally supplied tz-aware UTC timestamp from the information source.
- Abstain is ``None``, never a zero-stance Opinion.
- ``from __future__ import annotations`` — Python 3.11+.

Public surface
--------------
UnverifiedTip     — frozen dataclass for a raw tip.
TipSource         — ABC for tip-scraping adapters.
"""
from __future__ import annotations

import abc
import hashlib
from dataclasses import dataclass
from datetime import datetime


# ---------------------------------------------------------------------------
# UnverifiedTip
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UnverifiedTip:
    """A raw, unverified tip — NOT an Opinion.

    This is the primitive received from any external tip source (social feed,
    chat message, web scrape, alert service, etc.).  It carries zero evidential
    weight on its own.  Abstain is enforced downstream by the diversity gate.

    Fields
    ------
    ticker:
        Exchange ticker symbol referenced in the tip (e.g. "AAPL").
    claim:
        The raw claim text extracted from the source.  Never post-processed
        here; normalisation is the caller's responsibility.
    account:
        Source account identifier — a handle, username, or channel id.
        Used by ``account_scorer.py`` to assign a credibility score and by
        the diversity gate to distinguish sources.
    ts:
        Information timestamp (tz-aware UTC) when the tip was published or
        first observed.  NEVER ``datetime.now()`` — must come from the source.
    url:
        Canonical URL or message reference for the tip (for audit + replay).
    source_id:
        Identifier for the tip-source adapter that produced this tip
        (e.g. "twitter.v2", "reddit.wsb", "fintwit.scrape").  Used by the
        diversity gate to enforce independent corroboration — two tips from
        the same ``source_id`` are treated as one voice regardless of account.
    """

    ticker: str
    claim: str
    account: str
    ts: datetime
    url: str
    source_id: str

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def fingerprint(self) -> str:
        """Return a stable SHA-256 hex fingerprint for this tip.

        The fingerprint covers ticker, source_id, account, and url so that
        the same tip fetched twice produces the same fingerprint (dedup key).
        The claim text is intentionally excluded — minor edits/rewrites of the
        same post should still dedup.
        """
        blob = f"{self.ticker}|{self.source_id}|{self.account}|{self.url}"
        return hashlib.sha256(blob.encode()).hexdigest()

    def validate(self) -> None:
        """Raise ValueError on contract violations.

        Checks:
        - ticker is non-empty
        - claim is non-empty
        - account is non-empty
        - url is non-empty
        - source_id is non-empty
        - ts is tz-aware
        """
        errors: list[str] = []
        if not self.ticker:
            errors.append("ticker must be non-empty")
        if not self.claim:
            errors.append("claim must be non-empty")
        if not self.account:
            errors.append("account must be non-empty")
        if not self.url:
            errors.append("url must be non-empty")
        if not self.source_id:
            errors.append("source_id must be non-empty")
        if self.ts.tzinfo is None:
            errors.append("ts must be tz-aware (UTC)")
        if errors:
            raise ValueError("UnverifiedTip violations:\n" + "\n".join(f"  - {e}" for e in errors))


# ---------------------------------------------------------------------------
# TipSource ABC
# ---------------------------------------------------------------------------

class TipSource(abc.ABC):
    """Abstract base class for tip-scraping adapters.

    Concrete adapters (Twitter/X scraper, Reddit scraper, FinTwit scraper,
    alert service client, etc.) implement ``fetch()``.

    All adapters must:
    - Supply tz-aware UTC timestamps from the information source.
    - Never call ``datetime.now()`` — pass an ``as_of`` ceiling instead.
    - Return ``[]`` (not raise) on network errors in production; tests use
      mock adapters.
    - No network calls in unit tests (INTERFACES.md §11.6).
    """

    @property
    @abc.abstractmethod
    def source_id(self) -> str:
        """Unique stable identifier for this source adapter.

        Used by the diversity gate to distinguish independent sources.
        Two adapters with the same ``source_id`` count as one voice even if
        they return different accounts.

        Examples: "twitter.v2", "reddit.wsb", "stocktwits.api"
        """

    @abc.abstractmethod
    def fetch(
        self,
        ticker: str,
        as_of: datetime,
    ) -> list[UnverifiedTip]:
        """Return tips about *ticker* published at or before *as_of*.

        Parameters
        ----------
        ticker:
            Exchange ticker to search for.
        as_of:
            Information timestamp ceiling (tz-aware UTC).  The adapter MUST
            NOT return tips with ``ts > as_of`` (look-ahead guard mirrors the
            PITGateway contract).

        Returns
        -------
        list[UnverifiedTip]
            Tips sorted ascending by ``ts``.  May be empty.
        """
