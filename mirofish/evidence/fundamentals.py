"""Point-in-time fundamentals from SEC companyfacts.

Fetches the raw companyfacts dict via SecFactsClient, applies a load-bearing
`filed <= as_of` PIT filter (a fact filed AFTER as_of must NEVER be used),
derives the FundamentalFeatures, and computes pe/ps from the Alpaca last_close.
`valuation_z` comes from the vendored static sector table (a coarse heuristic).

NEVER raises -> None.

ISOLATION: pure stdlib + mirofish.types + mirofish.data. Never imports arbiter.
"""
from __future__ import annotations

from datetime import date, datetime

from mirofish.data.sector_valuation import pe_baseline_for
from mirofish.types import FundamentalFeatures, ensure_utc

# US-GAAP concept tags we look for, in priority order per field.
_REVENUE_TAGS = (
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "Revenues",
    "RevenueFromContractWithCustomerIncludingAssessedTax",
    "SalesRevenueNet",
)
_NET_INCOME_TAGS = (
    "NetIncomeLoss",
    "ProfitLoss",
)
_GROSS_PROFIT_TAGS = ("GrossProfit",)
_OPERATING_INCOME_TAGS = ("OperatingIncomeLoss",)
_SHARES_TAGS = (
    "WeightedAverageNumberOfDilutedSharesOutstanding",
    "WeightedAverageNumberOfSharesOutstandingBasic",
)
_DEI_SHARES_TAGS = ("EntityCommonStockSharesOutstanding",)

# Income-statement flow metrics are reported over many period lengths (3-month
# quarterly, 6/9-month YTD, 12-month annual). We want the ~annual figure so P/E
# / P/S denominators are a full trailing fiscal year, not a partial quarter.
_ANNUAL_MIN_DAYS = 340
_ANNUAL_MAX_DAYS = 400
_YOY_TOL_DAYS = 45


def _as_of_date(as_of: datetime) -> date:
    return ensure_utc(as_of).date()


def _parse_iso_date(raw: object) -> date | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def pit_concept_values(
    facts: dict,
    gaap_tag: str,
    as_of: datetime,
    *,
    unit: str = "USD",
    taxonomy: str = "us-gaap",
) -> list[dict]:
    """Return concept facts with `filed <= as_of`, newest period first.

    Pure helper. Each returned item is the raw fact dict (with `end`, `val`,
    `filed`, ...). The `filed <= as_of` filter is the load-bearing PIT gate.
    Sorted by `end` descending (newest reporting period first), then `filed`
    descending, so the latest-known value for the latest period wins.
    """
    as_of_d = _as_of_date(as_of)
    try:
        units = facts["facts"][taxonomy][gaap_tag]["units"]
    except (KeyError, TypeError):
        return []
    if not isinstance(units, dict):
        return []

    rows: list[dict] = []
    for unit_key, items in units.items():
        if unit is not None and unit_key != unit:
            continue
        if not isinstance(items, list):
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            filed = _parse_iso_date(it.get("filed"))
            if filed is None or filed > as_of_d:
                continue  # PIT exclusion: never use a fact filed after as_of.
            rows.append(it)

    def _sort_key(it: dict) -> tuple:
        end_d = _parse_iso_date(it.get("end")) or date.min
        filed_d = _parse_iso_date(it.get("filed")) or date.min
        return (end_d, filed_d)

    rows.sort(key=_sort_key, reverse=True)
    return rows


def _latest_value(
    facts: dict, tags: tuple[str, ...], as_of: datetime, *, unit: str = "USD"
) -> tuple[float | None, date | None, str | None]:
    """Latest PIT value across candidate tags. Returns (val, end_date, filed)."""
    for tag in tags:
        rows = pit_concept_values(facts, tag, as_of, unit=unit)
        for r in rows:
            val = r.get("val")
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            end_d = _parse_iso_date(r.get("end"))
            filed_raw = r.get("filed")
            filed_str = filed_raw if isinstance(filed_raw, str) else None
            return fval, end_d, filed_str
    return None, None, None


def _period_days(it: dict) -> int | None:
    """Length in days of a fact's reporting period (`end` - `start`), or None.

    Instantaneous facts (balance-sheet / share counts) have no `start` and
    return None — they must NOT be period-filtered.
    """
    start = _parse_iso_date(it.get("start"))
    end = _parse_iso_date(it.get("end"))
    if start is None or end is None:
        return None
    return (end - start).days


def _is_annual(it: dict) -> bool:
    pd = _period_days(it)
    return pd is not None and _ANNUAL_MIN_DAYS <= pd <= _ANNUAL_MAX_DAYS


def _latest_annual_value(
    facts: dict, tags: tuple[str, ...], as_of: datetime, *, unit: str = "USD"
) -> tuple[float | None, date | None, str | None]:
    """Latest PIT value over a ~ANNUAL period (trailing fiscal year ≈ TTM).

    Skips partial-period (quarterly / YTD) facts so flow metrics are full-year.
    Returns (val, end_date, filed). pit_concept_values already sorts newest
    period-end first, so the first annual hit is the latest fiscal year known
    as of `as_of`.
    """
    for tag in tags:
        for r in pit_concept_values(facts, tag, as_of, unit=unit):
            if not _is_annual(r):
                continue
            val = r.get("val")
            if val is None:
                continue
            try:
                fval = float(val)
            except (TypeError, ValueError):
                continue
            end_d = _parse_iso_date(r.get("end"))
            filed_raw = r.get("filed")
            filed_str = filed_raw if isinstance(filed_raw, str) else None
            return fval, end_d, filed_str
    return None, None, None


def _annual_value_for_period(
    facts: dict, tags: tuple[str, ...], as_of: datetime, target_end: date | None,
    *, unit: str = "USD",
) -> float | None:
    """Annual PIT value whose `end` is within ~45d of target_end (YoY pairing).

    Fiscal year-ends drift a few days year to year (52/53-week calendars), so we
    match on a tolerance rather than an exact date, and only across annual facts.
    """
    if target_end is None:
        return None
    best: float | None = None
    best_gap: int | None = None
    for tag in tags:
        for r in pit_concept_values(facts, tag, as_of, unit=unit):
            if not _is_annual(r):
                continue
            end_d = _parse_iso_date(r.get("end"))
            if end_d is None:
                continue
            gap = abs((end_d - target_end).days)
            if gap > _YOY_TOL_DAYS:
                continue
            if best_gap is None or gap < best_gap:
                try:
                    best = float(r["val"])
                    best_gap = gap
                except (KeyError, TypeError, ValueError):
                    continue
    return best


def compute_fundamentals(
    ticker: str,
    as_of: datetime,
    *,
    client,
    last_close: float | None,
    sector_map: dict[str, str],
) -> FundamentalFeatures | None:
    """Derive PIT fundamentals for `ticker` as of `as_of`. NEVER raises -> None."""
    try:
        facts = client.facts_as_of(ticker, as_of)
        if not isinstance(facts, dict):
            return None

        # Flow metrics: latest ~ANNUAL (trailing fiscal year) value, NOT a
        # partial quarter/YTD — otherwise pe/ps denominators are ~2x too small.
        revenue_ttm, rev_end, latest_filed = _latest_annual_value(facts, _REVENUE_TAGS, as_of)
        if revenue_ttm is None:
            # No annual US-GAAP revenue with filed<=as_of -> no coverage (abstain
            # rather than use a partial-period figure that would mis-value P/S).
            return None

        net_income_ttm, ni_end, ni_filed = _latest_annual_value(facts, _NET_INCOME_TAGS, as_of)
        gross_profit, _, _ = _latest_annual_value(facts, _GROSS_PROFIT_TAGS, as_of)
        operating_income, _, _ = _latest_annual_value(facts, _OPERATING_INCOME_TAGS, as_of)

        # Internal-consistency guard. Cross-company XBRL tag normalization is
        # imperfect (some filers report revenue/income under tags or period
        # structures our priority list mis-pairs), which can yield impossible
        # figures (e.g. net income > revenue). Rather than feed the LLM garbage,
        # abstain on fundamentals entirely and let the service degrade to a
        # technical-only opinion. (v2: per-filer tag normalization.)
        if (
            net_income_ttm is not None
            and revenue_ttm > 0.0
            and net_income_ttm > revenue_ttm
        ):
            return None

        # Shares: prefer diluted/basic (shares unit), fall back to DEI count.
        shares, _, sh_filed = _latest_value(
            facts, _SHARES_TAGS, as_of, unit="shares"
        )
        if shares is None or shares <= 0.0:
            shares, _, sh_filed = _latest_value(
                facts, _DEI_SHARES_TAGS, as_of, unit="shares"
            )

        # Revenue growth YoY: same calendar end one year earlier.
        revenue_growth_yoy: float | None = None
        if rev_end is not None:
            try:
                prior_end = rev_end.replace(year=rev_end.year - 1)
            except ValueError:
                prior_end = None
            rev_prior = _annual_value_for_period(facts, _REVENUE_TAGS, as_of, prior_end)
            if rev_prior not in (None, 0.0):
                revenue_growth_yoy = revenue_ttm / rev_prior - 1.0

        gross_margin = (
            gross_profit / revenue_ttm
            if gross_profit is not None and revenue_ttm not in (None, 0.0)
            else None
        )
        operating_margin = (
            operating_income / revenue_ttm
            if operating_income is not None and revenue_ttm not in (None, 0.0)
            else None
        )

        # Market-cap-derived ratios using Alpaca last_close.
        pe_ratio: float | None = None
        ps_ratio: float | None = None
        if last_close is not None and shares is not None and shares > 0.0:
            market_cap = last_close * shares
            if net_income_ttm is not None and net_income_ttm > 0.0:
                pe_ratio = market_cap / net_income_ttm
            if revenue_ttm > 0.0:
                ps_ratio = market_cap / revenue_ttm

        sector = sector_map.get(ticker.upper()) if sector_map else None

        valuation_z: float | None = None
        baseline = pe_baseline_for(sector)
        if pe_ratio is not None and baseline is not None:
            median_pe, stdev_pe = baseline
            if stdev_pe > 0.0:
                valuation_z = (pe_ratio - median_pe) / stdev_pe

        # Newest filed date used across the facts we consumed (audit; <= as_of).
        filed_candidates = [
            d for d in (latest_filed, ni_filed, sh_filed) if isinstance(d, str)
        ]
        as_of_latest_filed = max(filed_candidates) if filed_candidates else None

        return FundamentalFeatures(
            revenue_ttm=revenue_ttm,
            revenue_growth_yoy=revenue_growth_yoy,
            gross_margin=gross_margin,
            operating_margin=operating_margin,
            net_income_ttm=net_income_ttm,
            shares_outstanding=shares,
            pe_ratio=pe_ratio,
            ps_ratio=ps_ratio,
            sector=sector,
            valuation_z=valuation_z,
            as_of_latest_filed=as_of_latest_filed,
        )
    except Exception:
        return None
