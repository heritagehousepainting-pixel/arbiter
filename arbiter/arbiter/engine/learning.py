"""Learning-input assembly + cycle-opinion persistence (extracted from Engine).

Free functions taking the ``Engine`` instance as their first argument.  The
corresponding ``Engine`` methods are thin wrappers delegating here; behaviour
and the private method surface are unchanged.
"""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

import structlog

from arbiter.calibration.calibrator import Calibrator
from arbiter.calibration.multi_advisor import MultiAdvisorCalibrator
from arbiter.contract.opinion import Opinion
from arbiter.contract.seams import Idea
from arbiter.data.clock import BacktestClock
from arbiter.trust import store as trust_store
from arbiter.trust.ledger import TrustLedger
from arbiter.trust.weight_resolver import resolve_weight_bundle
from arbiter.types import HorizonBucket

if TYPE_CHECKING:
    from arbiter.engine._engine import Engine

log = structlog.get_logger(__name__)


def build_learning_inputs(engine: "Engine", now: datetime):
    """Build the (WeightBundle, calibrator) handed to ``fuse`` (sub-project #4).

    Runs once per FULL ``run_cycle`` (never in the daemon's fast iteration).
    Reads — never writes — outcomes (STRICT ``created_at < now`` cutoff, D0)
    and writes ``trust_weights`` + ``calibration_params``.

    LIVE mode (real ``Clock``): gate the heavy ledger ``update`` + calibrator
    ``fit`` on ``should_update`` (≥5 new outcomes); cache and reuse the
    ``(ledger_bundle, calibrator, cap_reasons)`` between updates.

    BACKTEST mode (``BacktestClock``, D2): recompute EVERY step — no
    cross-step cache (a cached bundle carries recency-decay computed at the
    OLD as_of and would not be the weight the live system had at this step).
    Warm-start / backtest reads use the as_of window (D4).
    """
    backtest = isinstance(engine.clock, BacktestClock)
    live_ids = list(engine.advisor_map.keys())
    floor = float(getattr(engine.config, "trust_equal_floor", 0.25))

    if engine.ledger is None:
        engine.ledger = TrustLedger()

    # PIT-safe inputs — the ONLY sanctioned assembler (strict < now, D0).
    outcomes_by_advisor = trust_store.load_outcomes_for_learning(engine.conn, now)

    ledger_bundle = None
    cap_reasons: dict[str, str | None] = {}
    calibrator: object

    if backtest:
        # D2: recompute every step, no cache.  Force the ledger so a
        # walk-forward replay re-derives the same weight the live system
        # would have had at this as_of.  update() returns None while dormant
        # (< activation threshold) → resolver falls back to the floor.
        ledger_bundle = engine.ledger.update(
            outcomes_by_advisor,
            eligible_by_advisor(engine, outcomes_by_advisor),
            as_of=now,
            force=engine.ledger.should_update(outcomes_by_advisor, now),
        )
        cap_reasons = dict(engine.ledger.last_cap_reasons)
        # Re-fit calibrators fresh from the cutoff list each step.
        calibrators: dict[str, Calibrator] = {}
        for advisor_id, records in outcomes_by_advisor.items():
            cal = Calibrator(advisor_id, conn=None)
            cal.fit([o for o, _ in records])
            calibrators[advisor_id] = cal
        calibrator = MultiAdvisorCalibrator(calibrators)
        if ledger_bundle is not None:
            trust_store.persist_weight_bundle(
                engine.conn, ledger_bundle, as_of=now, cap_reasons=cap_reasons
            )
    else:
        # LIVE: gate on should_update; cache otherwise.
        should = engine.ledger.should_update(outcomes_by_advisor, now)
        if should:
            ledger_bundle = engine.ledger.update(
                outcomes_by_advisor,
                eligible_by_advisor(engine, outcomes_by_advisor),
                as_of=now,
            )
            cap_reasons = dict(engine.ledger.last_cap_reasons)
            for advisor_id, records in outcomes_by_advisor.items():
                cal = engine.calibrators.get(advisor_id) or Calibrator(
                    advisor_id, conn=engine.conn
                )
                cal.fit([o for o, _ in records])
                engine.calibrators[advisor_id] = cal
                try:
                    cal.persist(as_of=now)
                except Exception as exc:  # noqa: BLE001
                    log.warning("engine.learning.calibrator_persist_failed", error=str(exc))
            calibrator = MultiAdvisorCalibrator(engine.calibrators)
            if ledger_bundle is not None:
                trust_store.persist_weight_bundle(
                    engine.conn, ledger_bundle, as_of=now, cap_reasons=cap_reasons
                )
            engine._learning_cache = (ledger_bundle, calibrator, cap_reasons)
        elif engine._learning_cache is not None:
            ledger_bundle, calibrator, cap_reasons = engine._learning_cache
        else:
            # No update yet this process — warm-start from persisted weights so a
            # restarted daemon doesn't fall back to all-cold (D7).  Calibrators
            # re-fit lazily on the next should_update.
            ledger_bundle = trust_store.load_latest_weight_bundle(
                engine.conn, now, backtest=False
            )
            cap_reasons = trust_store.load_cap_reasons(engine.conn, now, backtest=False)
            calibrator = MultiAdvisorCalibrator(engine.calibrators)

    # Apply the bootstrap floor / negative-skill suppression (D1/D3/D6).
    weight_bundle = resolve_weight_bundle(
        ledger_bundle, live_ids, equal_floor=floor, cap_reasons=cap_reasons
    )
    return weight_bundle, calibrator


def persist_cycle_opinions(
    engine: "Engine", now: datetime, valid_opinions: list[Opinion], ideas: list[Idea]
) -> None:
    """Persist each non-abstain opinion linked to its idea (#5a, D1).

    Links an opinion to an idea by typed (ticker, HorizonBucket) equality
    (E3).  An opinion matching no idea is persisted with ``idea_id=None``.
    Insert-only + idempotency SELECT-guard inside ``persist_opinion`` make a
    re-run at the same ``as_of`` safe.  A persist failure is logged AND
    counted via the ``attribution.opinion_persist_error`` metric (E1) — never
    silently swallowed — but does not abort the cycle.
    """
    from arbiter.signals import opinion_store  # noqa: PLC0415

    # Index ideas by (ticker, HorizonBucket) for typed-equality lookup (E3).
    ideas_by_key: dict[tuple[str, HorizonBucket], Idea] = {}
    for idea in ideas:
        try:
            bucket = HorizonBucket(idea.dedupe_key[1])
        except ValueError:
            continue
        ideas_by_key[(idea.ticker, bucket)] = idea

    for op in valid_opinions:
        try:
            op_bucket = op.horizon_bucket  # typed HorizonBucket
            matched = ideas_by_key.get((op.ticker, op_bucket))
            opinion_store.persist_opinion(
                engine.conn,
                op,
                idea_id=matched.idea_id if matched is not None else None,
                as_of=now,
                audit_path=engine.config.audit_path,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "engine.run_cycle.opinion_persist_failed",
                advisor_id=op.advisor_id, ticker=op.ticker, error=str(exc),
            )
            try:
                engine._metrics.record(
                    "attribution.opinion_persist_error",
                    {"advisor_id": op.advisor_id, "ticker": op.ticker, "error": str(exc)},
                    recorded_at=now.isoformat(),
                )
            except Exception:  # noqa: BLE001
                pass


def eligible_by_advisor(
    engine: "Engine", outcomes_by_advisor: dict
) -> dict[str, list[str]]:
    """v1 eligible-idea roster (D4): the set of idea_ids the advisor produced an
    outcome on (coverage ≈ 1.0).  A real roster (incl. abstained ideas) needs
    Lane-13 idea→advisor eligibility — out of scope for #4 (R2)."""
    return {
        advisor_id: [o.idea_id for o, _ in records]
        for advisor_id, records in outcomes_by_advisor.items()
    }
