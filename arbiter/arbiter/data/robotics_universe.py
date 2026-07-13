"""Canonical, pure-data robotics universe — the single source of truth.

Shared by BOTH the cockpit Robotics board (display, #2) and the robotics
early-insight signal (scan, #3).  Follows the ``data/fund_managers.py`` /
``data/activist_filers.py`` / ``data/sectors.py`` convention: a module-level
tuple of rows + thin accessors, ``from __future__ import annotations``, no I/O,
no network.

Each row carries ``symbol, company, layer, longevity, priceable, early_insight,
trigger, region, note, form_factors`` — the display fields the cockpit DTO needs
AND the per-name ``trigger`` (the "trigger to watch") the scanner keys on.

DISPLAY/DATA ONLY — this module CANNOT make a symbol trade-eligible.  It imports
nothing and never touches ``sectors.py`` / ``_DEFAULT_WATCHLIST``; only an
explicit, separately-reviewed edit to ``data/sectors.py::_SECTOR_BY_TICKER``
makes a ticker part of the ingest→trading funnel
(see docs/specs/2026-07-13-robotics-watchlist-design.md §7).

``priceable=True`` rows are US-listed / ADR symbols the cockpit ``/ticker`` +
``/chart`` endpoints can price; ``priceable=False`` rows are foreign-listed or
private chokepoints kept visible as tagged reference rows.
"""
from __future__ import annotations

from typing import Iterable

__all__ = [
    "GENERATED",
    "robotics_universe",
    "early_insight_names",
    "universe_by_symbol",
    "LAYERS",
]

GENERATED = "2026-07-13"

#: The stack layers, bottom (most concentrated) to top.
LAYERS: tuple[str, ...] = ("compute", "brain", "components", "integrator", "deployment")

# Each dict mirrors cockpit.api.contract.RoboticsRosterEntry.
_UNIVERSE: tuple[dict, ...] = (
    # ======================= compute =======================
    {"symbol": "NVDA", "company": "Nvidia", "layer": "compute", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["all"], "region": "US",
     "note": "Jetson Thor is the default robot-brain socket; compute+model+sim flywheel"},
    {"symbol": "TSM", "company": "TSMC", "layer": "compute", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["all"], "region": "Taiwan (US ADR)",
     "note": "leading-edge fab + CoWoS packaging — 'sold out through 2026'; the sector's toll booth"},
    {"symbol": "ARM", "company": "Arm Holdings", "layer": "compute", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["all"], "region": "UK (US ADR)",
     "note": "CPU IP inside most robot SoCs; stood up a dedicated Physical AI unit"},
    {"symbol": "QCOM", "company": "Qualcomm", "layer": "compute", "longevity": "durable",
     "priceable": True, "form_factors": ["mobility", "humanoid"], "region": "US",
     "note": "Snapdragon Ride (AV) + edge-compute challenger"},
    {"symbol": "AMD", "company": "AMD", "layer": "compute", "longevity": "durable",
     "priceable": True, "form_factors": ["all"], "region": "US",
     "note": "Western GPU/accelerator challenger to the Nvidia socket"},
    {"symbol": "688256.SS", "company": "Cambricon Technologies", "layer": "compute", "longevity": "unclear",
     "priceable": False, "form_factors": ["all"], "region": "China",
     "note": "domestic-GPU 'Nvidia substitute'; SMIC-node-constrained, state-backed"},
    {"symbol": "K-NPU", "company": "Korean NPU trio (Rebellions / FuriosaAI / DeepX)", "layer": "compute",
     "longevity": "unclear", "priceable": False, "form_factors": ["all"], "region": "South Korea",
     "early_insight": True,
     "trigger": "IPO pricing PLUS a design win in a shipping robot/vehicle (not just datacenter inference)",
     "note": "edge-inference layer of Korea's ~$1T national AI+robotics push"},

    # ======================= brain =======================
    {"symbol": "GOOGL", "company": "Alphabet / Google DeepMind (Gemini Robotics)", "layer": "brain",
     "longevity": "durable", "priceable": True, "form_factors": ["humanoid", "industrial"], "region": "US",
     "note": "Gemini Robotics VLA licensed into Atlas + Apptronik; owns Waymo + Intrinsic"},
    {"symbol": "MBLY", "company": "Mobileye", "layer": "brain", "longevity": "durable",
     "priceable": True, "form_factors": ["mobility"], "region": "Israel/US",
     "note": "Mobileye Drive autonomy stack (VW/MOIA ID.Buzz)"},
    {"symbol": "PHYSICAL-INTEL", "company": "Physical Intelligence", "layer": "brain", "longevity": "durable",
     "priceable": False, "form_factors": ["humanoid", "all"], "region": "US (private)",
     "early_insight": True,
     "trigger": "A marquee hardware maker licensing pi models as its production brain, or a paid enterprise deployment",
     "note": "Alphabet/CapitalG-led; builds no body — the invisible VLA layer researchers rate highest"},
    {"symbol": "SKILD", "company": "Skild AI", "layer": "brain", "longevity": "unclear",
     "priceable": False, "form_factors": ["humanoid", "all"], "region": "US (private)",
     "early_insight": True,
     "trigger": "A named blue-chip OEM shipping on 'Skild Brain', or revenue crossing ~$100M",
     "note": "SoftBank/Nvidia/Bezos/Samsung backing on 'one model, any body'; ~$0→$30M revenue"},
    {"symbol": "DYNA", "company": "Dyna Robotics", "layer": "brain", "longevity": "unclear",
     "priceable": False, "form_factors": ["logistics", "manipulation"], "region": "US (private)",
     "early_insight": True,
     "trigger": "First multi-site commercial contract or a named at-scale logistics customer",
     "note": "NVentures/Amazon/Samsung-backed manipulation model, 99%+ task success over 24h runs"},
    {"symbol": "AUTERION", "company": "Auterion", "layer": "brain", "longevity": "chokepoint",
     "priceable": False, "form_factors": ["drones"], "region": "US/Switzerland (private)",
     "early_insight": True,
     "trigger": "A NATO/US standardization or second nation-scale FPV block-buy naming Skynode as reference kit",
     "note": "'Android of military drones' — Skynode onto 50,000 Ukraine FPV drones + non-dilutive US OSC money"},
    {"symbol": "APPLIED-INT", "company": "Applied Intuition", "layer": "brain", "longevity": "durable",
     "priceable": False, "form_factors": ["mobility"], "region": "US (private)",
     "early_insight": True,
     "trigger": "A large defense/off-road autonomy award, or an IPO",
     "note": "$15B, embedded in 18 of the top 20 automakers; the validation/sim toll road under autonomy"},

    # ======================= components =======================
    {"symbol": "HSAI", "company": "Hesai Group", "layer": "components", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["humanoid", "mobility", "AMR"], "region": "China (US ADR)",
     "note": "#1 lidar; half of the Chinese lidar duopoly supplying the 'eyes' of most robots"},
    {"symbol": "ADI", "company": "Analog Devices", "layer": "components", "longevity": "durable",
     "priceable": True, "form_factors": ["humanoid", "all"], "region": "US",
     "note": "motion/sensor signal-chain ICs paired to robot compute"},
    {"symbol": "CGNX", "company": "Cognex", "layer": "components", "longevity": "durable",
     "priceable": True, "form_factors": ["industrial", "AMR"], "region": "US",
     "note": "machine-vision sensors/cameras for factory + logistics robots"},
    {"symbol": "6324.T", "company": "Harmonic Drive Systems", "layer": "components", "longevity": "chokepoint",
     "priceable": False, "form_factors": ["humanoid", "industrial"], "region": "Japan",
     "note": "invented + dominates strain-wave reducers (~14 per humanoid); the actuation chokepoint"},
    {"symbol": "6268.T", "company": "Nabtesco", "layer": "components", "longevity": "chokepoint",
     "priceable": False, "form_factors": ["industrial", "humanoid"], "region": "Japan",
     "note": "RV cycloidal reducer leader feeding FANUC/Yaskawa/ABB/KUKA"},
    {"symbol": "6481.T", "company": "THK", "layer": "components", "longevity": "durable",
     "priceable": False, "form_factors": ["humanoid", "industrial"], "region": "Japan",
     "note": "linear motion / bearings / ball screws"},
    {"symbol": "GSA/ROLLVIS", "company": "GSA (General Screw Arts) + Rollvis", "layer": "components",
     "longevity": "chokepoint", "priceable": False, "form_factors": ["humanoid"], "region": "Switzerland (private)",
     "early_insight": True,
     "trigger": "Optimus/other humanoid mass-production ramp confirmations, or capacity-expansion / long-term-supply deals",
     "note": "two Swiss firms hold 50%+ of planetary roller screws; ~14 per Optimus — the purest picks-and-shovels"},
    {"symbol": "MAXON", "company": "maxon group", "layer": "components", "longevity": "chokepoint",
     "priceable": False, "form_factors": ["surgical", "humanoid"], "region": "Switzerland (private)",
     "note": "39 precision micro-motors per da Vinci — a component vendor that gates the surgical leader"},
    {"symbol": "2498.HK", "company": "RoboSense", "layer": "components", "longevity": "durable",
     "priceable": False, "form_factors": ["humanoid", "AMR", "mobility"], "region": "China",
     "note": "other half of the Chinese lidar duopoly (AgiBot, Unitree, Galbot, Neura)"},
    {"symbol": "OPTIMUS-CN", "company": "Optimus China supply chain (Sanhua / Tuopu / Zhaowei / Xinjian / Mirle)",
     "layer": "components", "longevity": "unclear", "priceable": False, "form_factors": ["humanoid"],
     "region": "China/Taiwan (several Asia-listed)", "early_insight": True,
     "trigger": "Confirmed Optimus production volumes or follow-on component orders — order flow leads the OEM's economics",
     "note": "who actually gets paid when Optimus scales; Tesla's $685M Sanhua order named Zhaowei (hands) + Xinjian"},

    # ======================= integrator =======================
    {"symbol": "ISRG", "company": "Intuitive Surgical", "layer": "integrator", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["surgical"], "region": "US",
     "note": "10,670+ da Vinci installed base + switching-cost moat (gated upstream by maxon)"},
    {"symbol": "SYM", "company": "Symbotic", "layer": "integrator", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["warehouse"], "region": "US",
     "note": "absorbed Walmart's robotics arm + $5B+ backlog = largest retail-logistics deployment channel"},
    {"symbol": "AVAV", "company": "AeroVironment", "layer": "integrator", "longevity": "durable",
     "priceable": True, "form_factors": ["drones"], "region": "US",
     "note": "loitering munitions (Switchblade) + Skyfall/Shrike FPV; defense budgets underwrite it"},
    {"symbol": "TSLA", "company": "Tesla", "layer": "integrator", "longevity": "unclear",
     "priceable": True, "form_factors": ["humanoid", "mobility"], "region": "US",
     "note": "Optimus + robotaxi; enormous attention but ~20 robotaxis vs 'widespread' claims — binary"},
    {"symbol": "CAT", "company": "Caterpillar", "layer": "integrator", "longevity": "durable",
     "priceable": True, "form_factors": ["mining", "construction"], "region": "US",
     "note": "autonomous haul trucks — a shipping, revenue-real autonomy vertical"},
    {"symbol": "6954.T", "company": "FANUC", "layer": "integrator", "longevity": "durable",
     "priceable": False, "form_factors": ["industrial"], "region": "Japan",
     "note": "articulated-arm leader; the profitable installed base humanoids must displace"},
    {"symbol": "UNITREE", "company": "Unitree", "layer": "integrator", "longevity": "durable",
     "priceable": False, "form_factors": ["humanoid", "quadruped"], "region": "China (STAR IPO approved)",
     "note": "world #1 humanoid volume (~5,500 units 2025, ~$16k G1) — actually ships"},
    {"symbol": "FIGURE", "company": "Figure AI", "layer": "integrator", "longevity": "hype-risk",
     "priceable": False, "form_factors": ["humanoid"], "region": "US (private)",
     "note": "BMW deployment + Helix are real, but a ~$39B mark prices near-perfection"},
    {"symbol": "ANDURIL", "company": "Anduril", "layer": "integrator", "longevity": "chokepoint",
     "priceable": False, "form_factors": ["drones", "defense"], "region": "US (private)",
     "note": "~$20B Army Lattice deal = software backbone of US autonomous defense; a procurement moat"},
    {"symbol": "SHIELD-AI", "company": "Shield AI", "layer": "integrator", "longevity": "durable",
     "priceable": False, "form_factors": ["drones", "defense"], "region": "US (private)",
     "note": "Hivemind + V-BAT, $12.7B, Blackstone; underwritten by defense budgets not consumer cycles"},
    {"symbol": "DJI", "company": "DJI", "layer": "integrator", "longevity": "durable",
     "priceable": False, "form_factors": ["drones"], "region": "China (private)",
     "note": "~70-80% global drone share + deep vertical integration, but US/allied restrictions cap the market"},
    {"symbol": "NEURA", "company": "Neura Robotics", "layer": "integrator", "longevity": "durable",
     "priceable": False, "form_factors": ["humanoid"], "region": "Germany (private)",
     "early_insight": True,
     "trigger": "Bosch moving from co-development to production orders, or Neuraverse compounding across a deployed fleet",
     "note": "Germany's largest-ever raise (~$1.4B); Tether/Amazon/Nvidia/Qualcomm + a Bosch deal"},

    # ======================= deployment =======================
    {"symbol": "AMZN", "company": "Amazon (Amazon Robotics)", "layer": "deployment", "longevity": "chokepoint",
     "priceable": True, "form_factors": ["warehouse"], "region": "US",
     "note": "1M+ captive fulfillment robots; a closed vertical that sets the pace for logistics"},
    {"symbol": "GXO", "company": "GXO Logistics", "layer": "deployment", "longevity": "durable",
     "priceable": True, "form_factors": ["warehouse"], "region": "US",
     "note": "contract-logistics operator piloting humanoids + AMRs at scale"},
    {"symbol": "ROK", "company": "Rockwell Automation", "layer": "deployment", "longevity": "durable",
     "priceable": True, "form_factors": ["industrial", "AMR"], "region": "US",
     "note": "factory automation + OTTO AMR fleet orchestration"},
    {"symbol": "SERV", "company": "Serve Robotics", "layer": "deployment", "longevity": "unclear",
     "priceable": True, "form_factors": ["delivery"], "region": "US",
     "note": "sidewalk delivery — commoditizing chassis, value migrates to fleet ops"},
    {"symbol": "PONY", "company": "Pony.ai", "layer": "deployment", "longevity": "unclear",
     "priceable": True, "form_factors": ["mobility"], "region": "China (US ADR)",
     "note": "robotaxi/robotrucking — part of the Chinese AV cohort"},
    {"symbol": "WAYMO", "company": "Waymo (Alphabet)", "layer": "deployment", "longevity": "durable",
     "priceable": False, "form_factors": ["mobility"], "region": "US",
     "note": "the only robotaxi with real scaling paid volume (~500k rides/week); buy via GOOGL"},
    {"symbol": "MUJIN", "company": "Mujin", "layer": "deployment", "longevity": "chokepoint",
     "priceable": False, "form_factors": ["warehouse", "industrial"], "region": "Japan (private)",
     "early_insight": True,
     "trigger": "An IPO filing, or a US/European multi-warehouse rollout making it the default abstraction layer",
     "note": "'make any generic arm autonomous' controller software — boring, high-leverage, ignored"},
    {"symbol": "005380.KS", "company": "Hyundai Motor Group", "layer": "deployment", "longevity": "durable",
     "priceable": False, "form_factors": ["humanoid"], "region": "South Korea",
     "note": "owns Boston Dynamics + Mobis actuators + a 30k-unit Atlas factory — vertical integration"},
)


def robotics_universe() -> list[dict]:
    """Return the full curated universe as a list of plain dicts (one per name)."""
    return [dict(r) for r in _UNIVERSE]


def early_insight_names() -> list[dict]:
    """Return only the ⭐ early-insight rows (each carries a ``trigger`` to watch)."""
    return [dict(r) for r in _UNIVERSE if r.get("early_insight")]


def universe_by_symbol(symbols: Iterable[str] | None = None) -> dict[str, dict]:
    """Return ``{symbol: row}``; if ``symbols`` given, restrict to those (case-sensitive)."""
    wanted = set(symbols) if symbols is not None else None
    return {r["symbol"]: dict(r) for r in _UNIVERSE if wanted is None or r["symbol"] in wanted}
