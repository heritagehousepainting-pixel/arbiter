"""Account / posting-cluster credibility scorer — Lane 8.

SHADOW / DORMANT in MVP
-----------------------
Scores the account (or cluster of accounts) that posted a tip.  Low-credibility
or manipulative accounts down-weight the tip but never *alone* cause an
abstain — the diversity gate handles the corroboration threshold.

Scoring model (Phase-6 MVP — rule-based; Wave-C adds persistence)
-----------------------------------------------------------------
The scorer produces a float score in [0.0, 1.0]:
  1.0  = highly credible / verified institutional account
  0.5  = default / unknown account
  0.0  = flagged manipulator / bot / repeat pump account

Manipulation signals (each one fires a deduction):
  - Account is on the ``block_list`` (known manipulators) → score = 0.0
  - ``follower_count < MIN_FOLLOWERS`` → -0.2
  - Account age < MIN_ACCOUNT_AGE_DAYS → -0.2
  - ``pump_strike_count >= PUMP_STRIKE_THRESHOLD`` → score = 0.0 (capped floor)

The final score is clamped to [0.0, 1.0].

Account metadata is passed as a plain dict so that callers can populate it
from a DB query, API call, or test fixture without importing extra types.
The scorer does NOT call any external service or ``datetime.now()``.

Public surface
--------------
AccountScore          — result dataclass.
AccountScorer         — stateless scorer.
score_account()       — convenience function.

INTERFACES.md §11 — no ``datetime.now()``, no network in unit tests.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

_logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds (rule-based MVP; tune in Wave-C after forward-test data)
# ---------------------------------------------------------------------------

# Follower count below which the score is penalised.
MIN_FOLLOWERS: int = 100

# Account age (in days) below which the score is penalised.
MIN_ACCOUNT_AGE_DAYS: int = 90

# Number of prior pump/manipulation strikes at or above which the account is
# floored to 0.0 regardless of other signals.
PUMP_STRIKE_THRESHOLD: int = 2

# Deduction per individual signal (clamped to floor 0.0 after all deductions).
_DEDUCTION_LOW_FOLLOWERS: float = 0.2
_DEDUCTION_YOUNG_ACCOUNT: float = 0.2


# ---------------------------------------------------------------------------
# AccountScore
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AccountScore:
    """Result of scoring one account for credibility.

    Fields
    ------
    account:
        The account identifier that was scored.
    score:
        Credibility in [0.0, 1.0].  0.0 means flagged manipulator / zero
        trust; 1.0 means fully trusted.  Tips from 0.0-scored accounts are
        still collected (for the audit trail) but must be filtered by the
        diversity gate before contributing to a signal.
    reasons:
        Human-readable list of signals that fired (for audit / explainability).
    flagged:
        True iff the account hit any hard-floor trigger (block list or
        pump_strike_count >= threshold).
    """

    account: str
    score: float
    reasons: list[str] = field(default_factory=list)
    flagged: bool = False


# ---------------------------------------------------------------------------
# AccountScorer
# ---------------------------------------------------------------------------

class AccountScorer:
    """Stateless rule-based account credibility scorer.

    Parameters
    ----------
    block_list:
        Set of account identifiers that are known manipulators.  Accounts
        on the block list receive a hard score of 0.0 regardless of other
        signals.  Callers should load this from a DB or config file.
    """

    def __init__(self, block_list: frozenset[str] | None = None) -> None:
        self._block_list: frozenset[str] = block_list or frozenset()

    def score(
        self,
        account: str,
        *,
        as_of: datetime,
        follower_count: int = 0,
        account_created_at: datetime | None = None,
        pump_strike_count: int = 0,
    ) -> AccountScore:
        """Score *account* given the provided metadata.

        Parameters
        ----------
        account:
            Account identifier string (username / handle / user_id).
        as_of:
            Information timestamp (tz-aware UTC).  Used to compute account age;
            never ``datetime.now()`` — caller supplies from context.
        follower_count:
            Number of followers as of ``as_of``.  0 if unknown.
        account_created_at:
            Datetime when the account was created (tz-aware UTC), or None if
            unknown.
        pump_strike_count:
            Number of prior confirmed pump/manipulation strikes recorded for
            this account.

        Returns
        -------
        AccountScore
            Scored result with reasons list.
        """
        reasons: list[str] = []
        flagged = False

        # --- Hard floor: block list ---
        if account in self._block_list:
            reasons.append(f"Account {account!r} is on the manipulator block list")
            return AccountScore(
                account=account,
                score=0.0,
                reasons=reasons,
                flagged=True,
            )

        # --- Hard floor: pump strikes ---
        if pump_strike_count >= PUMP_STRIKE_THRESHOLD:
            reasons.append(
                f"Account has {pump_strike_count} pump strike(s) "
                f"(threshold: {PUMP_STRIKE_THRESHOLD})"
            )
            return AccountScore(
                account=account,
                score=0.0,
                reasons=reasons,
                flagged=True,
            )

        # --- Soft deductions ---
        base_score = 0.5  # default for unknown accounts

        if follower_count < MIN_FOLLOWERS:
            reasons.append(
                f"Low follower count ({follower_count} < {MIN_FOLLOWERS})"
            )
            base_score -= _DEDUCTION_LOW_FOLLOWERS

        if account_created_at is not None:
            if as_of.tzinfo is None:
                _logger.warning("as_of is naive — skipping account-age check")
            else:
                age_days = (as_of - account_created_at).days
                if age_days < MIN_ACCOUNT_AGE_DAYS:
                    reasons.append(
                        f"Young account ({age_days}d old < {MIN_ACCOUNT_AGE_DAYS}d threshold)"
                    )
                    base_score -= _DEDUCTION_YOUNG_ACCOUNT

        final_score = max(0.0, min(1.0, base_score))

        return AccountScore(
            account=account,
            score=final_score,
            reasons=reasons,
            flagged=flagged,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

def score_account(
    account: str,
    *,
    as_of: datetime,
    follower_count: int = 0,
    account_created_at: datetime | None = None,
    pump_strike_count: int = 0,
    block_list: frozenset[str] | None = None,
) -> AccountScore:
    """Score *account* using a default :class:`AccountScorer`.

    Convenience wrapper for one-off scoring without constructing a scorer.

    Parameters
    ----------
    account:
        Account identifier.
    as_of:
        Information timestamp (tz-aware UTC).
    follower_count:
        Follower count as of ``as_of``.
    account_created_at:
        Account creation datetime (tz-aware UTC), or None.
    pump_strike_count:
        Prior confirmed pump/manipulation strikes.
    block_list:
        Known-manipulator account set.

    Returns
    -------
    AccountScore
        Credibility score and reasons.
    """
    scorer = AccountScorer(block_list=block_list)
    return scorer.score(
        account,
        as_of=as_of,
        follower_count=follower_count,
        account_created_at=account_created_at,
        pump_strike_count=pump_strike_count,
    )
