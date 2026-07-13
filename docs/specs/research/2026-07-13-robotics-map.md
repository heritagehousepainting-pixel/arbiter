# The Robotics Economy: A Structural Map

*Synthesis as of 2026-07-13 · Aperture: all robot form factors, full stack · ~443 companies, ~626 dependency edges reviewed*

> **How to read this.** This map is a synthesis of two independent research leads — Lead A (supply chain / structure) and Lead B (capital / attention / longevity) — reconciled against their cross-reviews and a completeness critic. Where the leads disagreed or a reviewer flagged an error, the correction is folded in and marked. Confidence flags appear inline as **[high]**, **[medium]**, **[low]**. Facts are limited to what the inputs supplied; nothing is invented.

---

## 1. Executive summary

The robotics economy in 2026 is best understood not as a race between robot brands but as a **stack of layers**, where value and fragility concentrate in a handful of upstream nodes that almost nobody outside the industry talks about.

**The five sharpest takeaways:**

1. **Two dependency spines carry almost the entire map, and both pinch into a few firms.** The **compute/memory spine** (any robot brain → Nvidia Jetson Thor → Arm CPU IP + SK Hynix/Samsung HBM → TSMC fab + CoWoS packaging) and the **actuation spine** (every high-torque humanoid joint → precision reducers from Harmonic Drive Systems / Nabtesco + planetary roller screws from Switzerland's GSA / Rollvis). A shock to any of ~5 nodes propagates to nearly every form factor at once.

2. **The two spines have completely different capital dynamics — and this is the single most important insight the two leads produced jointly.** The compute spine is *self-funded* and being bought up by hyperscalers; the actuation spine is a *quiet, under-capitalized* chokepoint that **no hyperscaler is funding a fix for**. Humanoid scaling physically binds on reducers and roller screws long before it binds on GPUs. **[high]**

3. **Attention and traction are badly misaligned.** The loudest names (Figure at ~$39B, Tesla robotaxi/Optimus, 1X's NEO, demo-stage "brains") carry valuations years ahead of deployment. The quietest genuine winners — Waymo (~500k paid rides/week), Amazon Robotics (1M+ deployed), Intuitive Surgical (10,670+ installed systems), the reducer/roller-screw duopolies — are backed by *counts*, not clips.

4. **Government money is the biggest new force this cycle**, concentrated in defense drones/autonomy (the up-to-**$20B** US Army Anduril/Lattice deal is the pool's single largest check) and Asian national-champion industrial policy (Korea's ~$1T AI+humanoid push, China's state-backed compute and AgiBot). This reframes "commodity" airframes and "Nvidia-substitute" chips as **sovereign-underwritten durable-demand layers**, not race-to-zero commodities. **[high]**

5. **Vertical integration is the rational response to spine concentration.** Amazon, Tesla, and Hyundai each collapse layers 2–5 into one owner (Tesla in-house AI5 chip + captive Optimus supply chain; Hyundai owns Boston Dynamics + Mobis actuators + a 30k-unit Atlas factory; Amazon runs 1M+ captive robots). This is arguably a *more durable* model than the strategics' "buy the winning brain" bet.

---

## 2. The layer cake — who owns each layer

Read bottom-up as "who does what." The lower the layer, the more concentrated and the harder to substitute.

### Layer 0 — Foundry & memory (the true floor)
Invisible on any robot's spec sheet, but everything real-time rests on it.
- **Fab & advanced packaging:** **TSMC** (leading-edge nodes + **CoWoS** packaging — "sold out through 2026" per its CEO).
- **HBM memory:** **SK Hynix** (~58% share), **Samsung Electronics**, **Micron**.
- **China's constrained parallel floor:** **SMIC** (legacy nodes, output-capped) feeds the domestic compute bloc.

### Layer 1 — Compute (the silicon "nervous system")
- **The default socket:** **Nvidia Jetson Thor** (Blackwell, GA 2026) — designed into nearly every 2026 humanoid launch (Boston Dynamics Atlas, Figure, Apptronik, Agility, Unitree, AgiBot). **Arm** licenses the CPU IP inside most of these SoCs and stood up a dedicated Physical AI unit.
- **Western challenger field:** Qualcomm, AMD, Ambarella, TI, Intel, Broadcom, NXP, STMicroelectronics, Renesas (motor-control/BMS), Socionext; edge-AI startups Hailo, SiMa.ai, Axelera, plus Groq, Cerebras, Tenstorrent, EdgeCortix, Kneron, Blaize. *(The digest entry "Acrab" appears unverifiable and is likely a data error — **[low]**, treat as noise.)*
- **Korean NPU trio:** **Rebellions**, **FuriosaAI**, **DeepX** — the edge-inference layer of Korea's national bet.
- **China domestic-substitute bloc — two distinct sub-groups the leads initially conflated:** datacenter-GPU "Nvidia-killers" (**Cambricon**, **Moore Threads**, **Biren**) *and* the robotics/AV embodied-SoC makers (**Horizon Robotics**, **Black Sesame**). Both are SMIC-gated; the robotics-relevant names are the latter pair. **[medium]**
- **AV compute sub-vertical:** Nvidia DRIVE, Qualcomm Snapdragon Ride, Mobileye EyeQ.

### Layer 2 — Brain (foundation models / VLA / autonomy software)
The mind that turns compute into behavior.
- **Body-agnostic model vendors (license/open-source to OEMs):** **Physical Intelligence** (pi0/pi0.5), **Skild AI** (Skild Brain), **Nvidia Isaac GR00T** (+ Isaac Sim/Lab; **MuJoCo** the open physics-sim standard), **Google DeepMind Gemini Robotics**, **Dyna Robotics**, Genesis AI, Ant/LingBot, plus Field AI, Generalist AI, World Labs, Hugging Face (LeRobot). ⚠️ **Correction:** **Covariant** is *not* a live independent competitor — Amazon hired away its founders and took a non-exclusive model license (Aug 2024); treat it as **absorbed**, not active. **[high]**
- **In-house brains (integrators who build their own):** Figure (Helix), 1X (World Model), Sanctuary AI (Carbon), Boston Dynamics (now on Gemini), AgiBot (GO-2), UBTECH.
- **Logistics brains:** Mujin, Plus One, RightHand (picking), Brain Corp.
- **AV, where the brain *is* the product:** Waymo, Wayve, **Waabi** (⚠️ **trucking, not robotaxi** — see §3), **Nuro** (⚠️ now a **licensing/software** company (Nuro Driver), not a hardware OEM — see §3), Applied Intuition (sim/validation), Momenta, DeepRoute, Mobileye Drive, Helm.ai, Comma.ai; **Auterion** (Skynode) for drones.
- **Simulation / middleware / data tier:** Applied Intuition, Intrinsic, Isaac Sim, MuJoCo, Foretellix, Cognata, Parallel Domain, Duality AI, Viam, Open Robotics (ROS), Scale AI, SLAMcore.

### Layer 3 — Components (the mechatronic body — hardest to substitute)
- **Precision reducers:** strain-wave (**Harmonic Drive Systems** — note HD SE Germany and HDS Inc Japan are related-but-distinct legal entities) and RV cycloidal (**Nabtesco**, **Sumitomo Heavy Industries**). China price challengers: **Leaderdrive** / Zhongda Leader (Green Harmonic), Wuzhou Xinchun.
- **Planetary roller screws** (linear humanoid joints): Switzerland's **GSA + Rollvis** (50%+ world share); China's Xinjian, Sanhua, Zhaowei, Wuzhou Xinchun; plus Ewellix/SKF, Eichenberger as other Western sources.
- **Motors/actuators:** maxon, Kollmorgen, Moog, Allied Motion, Robotis, Renesas, Nidec, Faulhaber, WITTENSTEIN, **Hyundai Mobis** (Atlas), **Tuopu Group** (Optimus Tier-0.5 anchor).
- **Bearings & linear motion:** THK, NSK, SKF, Schaeffler, Timken, Hiwin.
- **Force-torque / tactile / dexterous hands:** ATI/Novanta, Bota, SCHUNK, Zimmer, OnRobot, TESOLLO, **Inspire Robots** (10k+ hands shipped 2025), **PaXini** (claims ~80% of humanoids use its tactile sensors — **[low]**, self-reported), LinkerBot, Shadow Robot, GelSight.
- **Perception sensors — lidar:** Hesai (#1), RoboSense, Innoviz, Luminar, Ouster, Aeva, Livox (DJI), Valeo, Continental; 3D vision: Orbbec, Intel RealSense, Photoneo, Mech-Mind, Cognex, Keyence, SICK; radar/IMU: Arbe, TDK-InvenSense, Bosch Sensortec.
- **Power:** Samsung SDI, CATL, LG Energy Solution, Panasonic Energy, BYD.
- **Motion/sensor chips paired to Thor:** Infineon, Analog Devices.

### Layer 4 — Integrator (the OEM who builds the robot)
See the full form-factor tour in §3. In brief: industrial arms (FANUC, ABB, Yaskawa, KUKA, + the Japanese "Big 5" Nachi, Denso Wave, Seiko Epson, Mitsubishi Electric, Kawasaki, Omron), cobots (Universal Robots/Teradyne, Techman, Doosan; China's Dobot/AUBO/JAKA), warehouse (Symbotic, AutoStore, Ocado, Geek+, Locus, Exotec, Dematic/KION), humanoid OEMs, surgical, drones, AV.

### Layer 5 — Deployment (who owns the workflow & end demand)
- **Fleet/RaaS & orchestration:** Intrinsic ("Android of robotics"), Vention, Formic, InOrbit, Formant, Rockwell, Dexory.
- **End operators who pull robots into a P&L:** Amazon Robotics (1M+ units, in-house), Walmart/Target (via Symbotic), BMW/Mercedes/Hyundai/GXO/Magna (humanoid pilots), Waymo/Tesla/Baidu/Zoox (robotaxi fleets), Zipline/Wing (delivery), and the militaries buying drones (materially under-counted at this layer — see §4/§5).
- **Vertical integrators collapse Layers 2–5:** Amazon, Tesla, Hyundai/Boston Dynamics.

---

## 3. Form-factor tour

### Industrial arms & cobots — the profitable installed base
The revenue backbone of robotics *today*, and the factory-floor channel humanoids must displace. **FANUC, ABB, Yaskawa, KUKA** (⚠️ KUKA is Chinese-owned by **Midea** — a material capital/geopolitical fact) anchor articulated arms; the Japanese "Big 5" extend via **Nachi-Fujikoshi, Denso Wave, Seiko Epson** (SCARA leader), **Mitsubishi Electric, Kawasaki**; **Comau, Stäubli, HD Hyundai Robotics** round out the West. **Cobots:** Universal Robots (Teradyne) leads, but three Chinese makers (**Dobot, AUBO, JAKA**) now sit in the global cobot top 4 on price — the arm itself is commoditizing beneath UR. China industrial arms: Estun, Inovance, SIASUN, EFORT. *Nabtesco's reducer moat exists precisely because it feeds FANUC/Yaskawa/ABB/KUKA — you cannot reason about the actuation chokepoint without this layer.* **[high]**

### Warehouse / AMR
**Symbotic** (bought Walmart's robotics arm, $5B+ backlog) and **Amazon Robotics** (1M+ captive) dominate the deployment channel. **AutoStore** + **KION/Dematic** control the grid-ASRS platform resold into grocery/retail fulfillment — a gate position. **Ocado** is a parallel grid platform. Commodity mid-field where value is *ops, not chassis*: Geek+, Locus, Exotec, GreyOrange, Hai, Fetch/Zebra, OTTO/Rockwell, Seegrid. Picking brains: Mujin, Plus One, RightHand, Pickle, Chef, Ambi. ⚠️ **Correction:** Amazon is *not* a purely "closed vertical" — it paused the Agility Digit trial but backs Neura, owns Zoox, runs an Industrial Innovation Fund (Dyna), and absorbed Covariant's team. It is buying broad external optionality. **[high]**

### Drones (commercial, delivery, defense)
**DJI** holds ~70–80% global share with deep vertical integration, but US/allied restrictions structurally cap its addressable market and seed NDAA-compliant rivals (Skydio, Parrot, Anduril, Vantage). **Defense/attritable:** Anduril, Shield AI (Hivemind + V-BAT), AeroVironment (Switchblade), Neros (Archer), Quantum Systems, plus primes General Atomics, Northrop, Kratos, Elbit, Baykar, Insitu. **The brain inside the airframe:** **Auterion Skynode** (see watchlist). **Delivery:** Zipline, Wing, Wingcopter, Matternet, Flytrex. ⚠️ **Correction:** "**AV Skyfall (Ukraine)**" in the source data conflates two things — **AeroVironment** (US; Skyfall/Shrike FPV line via BlueHalo) and **Auterion's** separate direct 50,000-unit Ukraine Skynode block-buy. They should be split. **[high]**

### Surgical
**Intuitive Surgical** (10,670+ installed da Vinci systems) has a switching-cost moat, but **both sides are more nuanced than "near-unassailable":**
- **Upstream dependency:** Intuitive itself is gated by **maxon**, which supplies **39 precision micro-motors per da Vinci** — a single Swiss component vendor sits under the market leader. **[high]**
- **Credible challengers with named strategic parents** (not startups): Medtronic (Hugo), J&J MedTech (Ottava), Stryker, Zimmer Biomet, CMR Surgical (Versius), Distalmotion. Plus a deep long tail: PROCEPT, Globus, Noah Medical, Stereotaxis, Corindus/Siemens Healthineers, MicroPort MedBot, and China's Edge/Ronovo/SS Innovations.

### Autonomous mobility (robotaxi, trucking, delivery)
- **Robotaxi:** **Waymo** is the clear survivor (~500k paid rides/week; ⚠️ its "200M+ autonomous miles" figure may conflate cumulative vs rider-only — flag for precision, **[medium]**). Tesla, Zoox (Amazon-owned), Baidu Apollo Go, plus the **entire Chinese cohort the leads initially omitted: Pony.ai, WeRide, AutoX, DiDi**. Mobileye Drive powers VW/MOIA's ID.Buzz. GM Cruise, Motional, May Mobility fill out shuttles.
- **Trucking / middle-mile:** **Aurora, Kodiak, Waabi** (⚠️ trucking, *not* robotaxi — Waabi Driver targets driverless long-haul on the Uber Freight network), Torc (Daimler), Gatik (middle-mile), Einride, Inceptio.
- **Licensing structures = captive supply:** **Nuro** licenses Nuro Driver into the **Lucid + Uber 20,000-vehicle** deal (software, not hardware). OEMs and Uber are now *co-funding* autonomy to lock supply, not just to invest — Nuro-Lucid-Uber, Waabi-Uber, the Wayve syndicate. **[high]**
- **Sidewalk delivery (commoditizing chassis):** Serve, Starship, Coco, Cartken, Ottonomy.

### Humanoid — the most-hyped square
- **Ships in real volume:** **Unitree** (world #1 at ~5,500 units in 2025, ~$16k G1, STAR Market IPO approved), **AgiBot** (0→10,000 units in months, HK IPO).
- **Western front-runners:** **Figure** (BMW deployment + Helix, $39B), **Apptronik** (Apollo, Mercedes), **Agility** (Digit), **Boston Dynamics** (Hyundai + Gemini, CES 2026 "Best Robot"), **1X** (NEO, 10k pre-orders), Sanctuary, Kepler.
- **The broad Chinese wave the leads under-covered:** Fourier Intelligence, Galbot (~$3B val), RobotEra, Astribot, LimX, XPeng (IRON), Xiaomi (CyberOne), EngineAI, PNDbotics, MagicLab, Booster, plus quadrupeds Deep Robotics, ANYbotics, Ghost.
- **Europe:** **Neura Robotics** (Germany's largest-ever raise ~$1.4B; belongs here as an OEM, not merely a lidar customer), PAL, Pollen, Mentee.
- **Contract manufacturing tier:** **Foxconn/Hon Hai** is now an Nvidia-partnered humanoid manufacturer (VivaTech 2026); Jabil, Flex, Quanta.
- ⚠️ **Tesla Optimus** is a top-3 humanoid program and anchors the entire "T-chain" supply map — it must carry a humanoid node, not only an AV node.

### Segments the leads missed entirely (completeness critic)
Flagged as real deployment verticals absent from both drafts, included for completeness **[medium]**: **Agriculture** (John Deere/Blue River, Kubota, Monarch, Carbon Robotics, XAG), **Construction/mining** (Built, Dusty, Caterpillar, Komatsu, Sandvik, Epiroc), **Exoskeletons/rehab** (Wandercraft, Ekso, Cyberdyne, German Bionic, Ottobock, Fourier), **eVTOL/air mobility** (Joby, Archer, Beta, Wisk, EHang, AutoFlight — as of 2026 only EHang and AutoFlight hold full type certificates), **Consumer/service** (iRobot, Ecovacs, Roborock, Dreame, Pudu, Keenon, Bear, Diligent, Knightscope), and **maritime autonomy** (Saronic, Saildrone, Ocean Infinity, Kongsberg).

---

## 4. Supply chain: who supplies whom

### Spine 1 — the compute/memory spine (silicon)
Trace any 2026 humanoid or AV brain and it converges:

> **Humanoid OEM** (Boston Dynamics · Figure · Apptronik · Agility · Unitree · AgiBot · UBTECH) **→ Nvidia Jetson Thor / GR00T → Arm** (CPU IP inside the SoC) **+ SK Hynix & Samsung** (HBM) **→ TSMC** (fab + CoWoS packaging).

**The pinch is CoWoS** — one Taiwanese packaging line, sold out through 2026, rate-limits every downstream robot brain, backstopped by a Korean HBM duopoly. Nvidia also **funds its own demand** (NVentures into Skild, Wayve, Waabi, Dyna, Neura) and sits in the AV spine twice (DRIVE silicon *into* Wayve, which it also invests in). China runs a parallel, weaker, SMIC-capped spine (Horizon/Black Sesame/Cambricon/Moore Threads/Biren) that persists **because of state backing, not unit economics** — a policy bet, not a market one. **[high]**

### Spine 2 — the actuation spine (mechatronics) — where humanoids physically bind
Every high-torque joint needs a precision reducer; every linear joint needs a roller screw. This is a **second, independent chokepoint pool** — and crucially, **no hyperscaler is funding a fix for it**, unlike the compute pinch.

> **Tesla Optimus →** 14 **Harmonic Drive** strain-wave reducers + 14 **GSA** roller screws per unit, with Sanhua ($685M order), Tuopu, Xinjian, Mirle, Zhaowei (hands) as the multi-sourced China tier.

⚠️ **Two corrections to how the leads framed this:**
- **"Reducer duopoly" conflates two distinct products.** Harmonic Drive = strain-wave; Nabtesco = RV cycloidal (Sumitomo a third RV player) — they dominate *different joint types*. It is **two adjacent near-monopolies, not one shared duopoly.** **[high]**
- **Pricing power is eroding, not fixed.** Combined HDS + Nabtesco *value* share is slipping from ~85% toward ~45–55% as Leaderdrive (60% China reducer share) and Wuzhou Xinchun (roller screws at ~half Rollvis's cost) undercut ~50%. Near-term *necessity* remains; long-term *margin* does not. **[medium]** Roller screws are a concentrated **oligopoly**, not a strict duopoly (Ewellix/SKF, Eichenberger, Chinese entrants exist — the reason Tesla multi-sources).

### Cross-cutting chains worth naming
- **Surgical:** maxon → Intuitive (39 motors/da Vinci) — a component vendor gates the market leader.
- **Perception is a Chinese lidar duopoly** supplying the "eyes" of *both* Western and Chinese robots: **Hesai** (→ all Unitree humanoids) and **RoboSense** (→ AgiBot, Unitree, Galbot, Agile, Neura). This is a single-point-of-failure for perception that carries the **same US/allied-restriction geopolitical risk as the TSMC/HBM pinch.** Western alternatives: Innoviz (→ Mobileye/VW, up to 9 lidars/vehicle). **[high]**
- **Brain-as-licensed-input:** Gemini Robotics → Atlas + Apptronik; Figure Helix → BMW's 40 Figure-03 units; Auterion Skynode → FPV mass.
- **Power layer** (absent from the capital narrative, real cost/sourcing/differentiation): CATL/LG/Samsung SDI/Panasonic — commodity volume cells vs premium/solid-state pricing power.

### Where the chain concentrates (the ~5 nodes that matter most)
**TSMC-CoWoS · SK Hynix HBM · Nvidia (default socket) · Arm (IP under the socket) · the Japanese-reducer + Swiss-roller-screw actuation duopolies.** A shock to any one propagates to nearly every form factor simultaneously.

### Commodity layers (where value migrates *away* from the visible product)
FPV airframes (value → Auterion autonomy kit), sidewalk delivery chassis (value → fleet ops), mid-tier Chinese cobots, Chinese reducers/roller screws (low end), volume batteries, standard grippers/EOAT, automotive lidar mid-field. ⚠️ **But** — the FPV airframe and "Chinese Nvidia-substitute" tiers are **underwritten by sovereign buyers** (defense procurement / state industrial policy). A commodity backed by a state buyer behaves like a **durable-demand layer, not a race-to-zero one.** The commodity framing must be qualified. **[high]**

---

## 5. Capital: who's backed heaviest (follow the money)

Five pools, ranked by depth:

**1. The self-funding compute oligopoly (deepest, needs no outside money).** Nvidia, TSMC, SK Hynix, Samsung, Micron sit upstream of every form factor and fund scaling from cash flow. *They are the house — everyone below pays rent.*

**2. Government money — the single biggest NEW force this cycle.**
- *Defense drones/autonomy:* US Army **up-to-$20B / 10-year Anduril/Lattice** deal (largest single check in the pool); **Shield AI** ($1.5B Series G at $12.7B + $500M Blackstone, $800M Navy competition); **Skydio** ($3.5B US manufacturing plan, largest-ever Army sUAS order); AeroVironment ($990M IDIQ); Neros; **Auterion** ($25M non-dilutive US Office of Strategic Capital); **Quantum Systems** ($1.2B Series D at ~$8B).
- *National industrial policy:* **Korea's ~$1T Samsung-led AI+humanoid push** (K-Humanoid Alliance, Rainbow Robotics under Samsung, Rebellions' $400M); China's state backing of AgiBot + the domestic-GPU cohort; **Hyundai's $26B US investment** incl. a 30k-unit/yr Atlas factory.

**3. Strategic corporate money — crowding one square: the humanoid/robot-foundation-model layer.** The same ~7 balance sheets recur, and **their cross-ownership means Layer 2's apparent independence is largely illusory** — these are optionality bets held by the players who *also* own Layers 0–1:
- **SoftBank** (leads Skild's $1.4B *and* owns **Arm** — a structural coupling: the same firm holds the model bet and the IP under every SoC), Nvidia/NVentures (everywhere), **Bezos** (Figure, Skild), **Samsung** (Rebellions, Skild, Rainbow, Dyna via Next), **Amazon** (Neura, Zoox, Dyna, absorbed Covariant), **Microsoft + OpenAI** (Figure; 1X), **Alphabet/CapitalG** (Waymo, Intrinsic, Wing, Physical Intelligence). *When five of the world's richest strategics buy the same layer, that layer is where the perceived winner-take-most prize sits.* **[high]**

**4. Frothy venture — pricing the "brain" ahead of revenue.** Figure ~$39B Series C; **Skild** tripling to $14B in 7 months on ~$30M live revenue; **Physical Intelligence** reportedly doubling to ~$11B in four months; **Neura** ~$1.4B (record for Germany); Apptronik $520M at $5B. Capital is buying optionality on "the omni-body model that wins."

**5. AV/robotaxi — most capital-intensive, now more disciplined and strategically anchored.** Waymo (Alphabet-funded, real forming unit economics), Wayve ($1.2B at $8.6B, all-star syndicate), Waabi ($1B incl. Uber, largest Canadian raise ever), Nuro-Lucid-Uber (20,000-vehicle licensing). *Pattern: OEMs and Uber co-fund software to **lock supply**, not merely to invest.*

**The structural point the capital narrative under-drew:** vertical integration (Amazon, Tesla, Hyundai) **is the capital-allocation reaction to Spine 1 and Spine 2 concentration** — internalizing margin and de-risking the chokepoints. It changes who is really "backed heaviest": a self-funded integrator that owns its supply chain may be more durable than a $39B brain bet. **[high]**

---

## 6. Attention: who people are talking about

**The master narrative is Nvidia's "Physical AI"** — Jetson Thor GA + design-ins across nearly every 2026 humanoid launch. This is *genuine* traction (design wins + revenue, not just press).

### The "Nike of robotics" arms race — three loud fronts
- **Humanoids (loudest):** Figure, Tesla Optimus (relentless Musk attention), Unitree (genuine volume leader), AgiBot, Boston Dynamics Atlas, Apptronik/Mercedes, 1X NEO.
- **Robot brains (high researcher/VC attention, low public awareness):** Physical Intelligence, Skild ("one model for any body"), Gemini Robotics, Nvidia GR00T.
- **Defense drones:** Anduril and Shield AI now mainstream business-press names; DJI the ~70–80% incumbent everyone references; Neros/Auterion/Quantum ride the Ukraine-FPV story.

### The "Nike of robotics" candidates — and the honest read
No single winner exists yet. The strongest *traction-backed* claimants are **Nvidia** (owns the socket), **Unitree** (owns the volume narrative), and **Waymo** (owns the only real robotaxi unit economics). The strongest *narrative* claimants (Figure, Tesla Optimus) carry the widest gap between story and shipped units.

### Genuine traction vs hype — the key discipline
- **Real, under-hyped relative to substance:** Waymo (~500k paid rides/week — the quietest big winner), Amazon Robotics (1M+), Intuitive (10,670+ installed), Symbotic ($5B+ backlog), Hesai/RoboSense (deliveries up 170–510% YoY), Unitree/AgiBot (units shipped). *Buzz backed by counts.*
- **Hype running ahead of reality:** **Tesla robotaxi** (Musk: "widespread by end of 2026" vs ~20 vehicles in Austin); **1X NEO** (60–70% autonomy at launch, human teleoperators in "Expert Mode" backfilling 30–40% of tasks — the "autonomous home robot" framing is generous); **Genesis AI** (demos, no deployment); the **China domestic-GPU** "Nvidia-killer" story (Moore Threads +243%, Biren IPO, Cambricon ~$83B EV) that outruns SMIC-constrained physical output.

### Attention absent but shouldn't be
The boring chokepoints nobody tweets about: the reducer near-monopolies (Harmonic Drive, Nabtesco), the roller-screw oligopoly (GSA, Rollvis), controller software (Mujin, Intrinsic), and the maxon → Intuitive dependency. **Zero consumer buzz, maximum structural leverage.**

---

## 7. Longevity verdicts

| Company / node | Verdict | Why |
|---|---|---|
| **Nvidia** | Durable winner / chokepoint | Compute + model + sim flywheel (Thor, GR00T, Isaac) is the default; NVentures buys the layer above. Hardest position to dislodge. |
| **TSMC / SK Hynix / Samsung** | Durable chokepoint | Every leading robot brain is fabbed here and fed by HBM. CoWoS sold out through 2026 = the sector's toll booth regardless of which robot wins. |
| **Waymo** | Durable winner | Only autonomy player with real, scaling paid volume (~500k/week) + Alphabet balance sheet. Robotaxi's clearest survivor. |
| **Harmonic Drive / Nabtesco / GSA / Rollvis** | Durable chokepoints (quiet) | Picks-and-shovels every humanoid needs (~14 reducers + ~14 roller screws/robot). Chinese undercutting erodes share/margin but not near-term necessity. **No hyperscaler funding a bypass.** |
| **Anduril** | Durable chokepoint (defense) | ~$20B Army enterprise deal around Lattice = software backbone of US autonomous defense; a procurement moat you can't out-VC. |
| **Shield AI** | Durable | Hivemind + V-BAT, $12.7B, Blackstone, multi-government contracts. Defense budgets, not consumer cycles, underwrite it. |
| **Boston Dynamics** | Durable | Hyundai balance sheet + Gemini brain + committed 2026 Atlas production. Best-capitalized Western humanoid path. |
| **Symbotic** | Durable / chokepoint | Absorbed Walmart's robotics business + $5B+ backlog = largest retail-logistics deployment channel. |
| **Intuitive Surgical** | Durable incumbent — *with an upstream dependency* | 10,670+ installed base + switching costs. But gated by **maxon** upstream, and Medtronic/J&J/CMR are *strategically-parented* challengers, not startups. |
| **Unitree** | Durable volume winner | Actually ships (world #1, low-cost G1) + real IPO. Low-end commoditization is the flip side of cost leadership. |
| **Mujin / Intrinsic** | Durable chokepoint (underrated) | "Make any arm autonomous" software with real economics (Mujin doubling sales; Intrinsic inside Google). Low buzz, high leverage. |
| **DJI** | Durable but geopolitically capped | ~70–80% share + vertical integration, but US/allied restrictions structurally cap the market and seed NDAA-compliant rivals. |
| **Figure AI** | Front-runner but **valuation-fragile** | Real BMW deployment + Helix are genuine, but a $39B mark prices near-perfection. Widest downside if scaling slips. |
| **Skild AI** | High-upside, **must convert** | Elite backing + $0→~$30M revenue, but $14B on 7 months of froth needs **named OEM design wins** to hold. |
| **Physical Intelligence** | Durable *if it becomes the layer* | Best-regarded generalist VLA (open-sourced pi0 for lock-in), but $11B assumes it becomes the standard others license — unproven commercially. |
| **Tesla (robotaxi/Optimus)** | **Unclear — narrative ahead of reality** | Enormous attention + China-heavy Optimus supply chain, but ~20 robotaxis vs "widespread" claims and unproven Optimus mass production make it binary. |
| **1X Technologies** | **Hype-risk on autonomy** | 10k pre-orders are real demand, but teleoperated "Expert Mode" backfilling 30–40% of tasks means the autonomous-home promise is unproven at ship. |
| **China domestic-GPU cohort** (Cambricon, Moore Threads, Biren) | **Unclear / policy-driven** | Real revenue growth, but SMIC node constraints cap physical delivery. Won't die on economics (state-backed) — but can't fully deliver the narrative either. |
| **Genesis AI / demo-stage names** | **Hype-risk** | Impressive demos or single orders without durable deployment. Attention borrowed from the category, not earned. |

---

## 8. Early-insight watchlist — pre-mainstream names

Names where smart/strategic money is positioned but public awareness lags, plus the concrete **trigger** that would confirm the thesis.

| Company | Why early | Trigger to watch |
|---|---|---|
| **Auterion** (private) | The "Android of military drones" — Skynode onto 50,000 Ukraine FPV drones, DoD adoption, rare non-dilutive US OSC money. Public knows the drone makers, not the brain inside. | A NATO/US standardization or second nation-scale FPV block-buy naming Skynode as reference kit — converts vendor → standard. |
| **Skild AI** (private) | SoftBank/Nvidia/Bezos/Samsung/LG/Schneider/Salesforce all in on "one model, any body"; already $0→~$30M revenue; near-zero public awareness vs Figure/Tesla. | A named blue-chip OEM shipping on "Skild Brain," or revenue crossing ~$100M — proof the omni-body thesis is *bought*, not just funded. |
| **Physical Intelligence** (private) | Alphabet/CapitalG led; valuation reportedly doubled to ~$11B in 4 months; builds *no body* — the invisible layer researchers rate highest; open-sourced pi0 (classic pre-standard move). | A marquee hardware maker licensing pi models as its production brain, or a paid enterprise deployment. |
| **GSA + Rollvis** (private) | Two Swiss firms hold 50%+ of planetary roller screws; Tesla buys 14 per Optimus — a hard physical chokepoint with almost no investor awareness. The ultimate picks-and-shovels. | Optimus (and other humanoid) mass-production ramp confirmations, or capacity-expansion/long-term-supply announcements — *before* Chinese undercutters (Wuzhou Xinchun, Xinjian) compress the window. |
| **Dyna Robotics** (private) | NVentures/Amazon/Samsung Next/Salesforce backed a manipulation model at 99%+ task success over 24h continuous runs — the *reliability* bar that unlocks commercial deployment. Very low profile. | First multi-site commercial contract or named at-scale logistics customer. |
| **Mujin** (private) | Profitable-trajectory controller software ("make any generic arm autonomous"), sales doubling and doubling, raising toward a $233M Series D + IPO by 2030. Boring, high-leverage, ignored. | IPO filing, or a US/European multi-warehouse rollout making it the default abstraction layer for mixed-brand arms. |
| **Neura Robotics** (private) | Germany's largest-ever round (~$1.4B) — Tether, Amazon, Nvidia, Qualcomm + a Bosch co-development deal. Europe's best-funded humanoid; Neuraverse targets fleet network effects. Under-followed in US media. | Bosch moving from co-development to production orders, or evidence Neuraverse is compounding across a deployed fleet. |
| **Applied Intuition** (private) | $15B, BlackRock/Kleiner-backed, embedded in 18 of the top 20 automakers, quietly expanding into defense/off-road — the validation/sim toll road under many autonomy programs. Enterprise-invisible. | A large defense/off-road autonomy award, or an IPO. |
| **Korean NPU trio** (Rebellions / FuriosaAI / DeepX) | Samsung-backed Rebellions ($400M, KOSPI IPO), Furiosa, DeepX (~$700M IPO planned) — the edge-inference layer of Korea's ~$1T national push. A whole national bet the US public isn't watching. | IPO pricing **plus** a design win in a *shipping robot/vehicle* (not just datacenter inference) — confirms the robotics-edge thesis, not AI-chip hype. |
| **Optimus China supply chain** (Sanhua, Tuopu, Zhaowei, Xinjian, Mirle) — *mixed; several Asia-listed* | Tesla's $685M Sanhua order + naming of Zhaowei (hands) and Xinjian (roller screws, 1M-unit line) reveals who actually gets paid when humanoids scale. Visible in Asian listings, off most Western radars. | Confirmed Optimus production volumes or follow-on component orders — order flow leads the robot maker's own economics. |

---

## 9. Caveats & confidence notes

**Method.** This document synthesizes two research leads, their cross-reviews, and a completeness critic. Where a reviewer corrected a lead, the correction is marked ⚠️ inline. Where confidence is stated, it reflects the leads' own hedging plus reviewer scrutiny.

**Specific figures to treat with caution:**
- **Figure "~$25/robot-hour" BMW pricing** is an *unconfirmed press estimate*, and the engagement is a ~40-unit Figure-03 Spartanburg deployment (pilot/early-commercial scale) — cite as a real deployment, **not** a proven commercial supply contract at that price. **[low on the price number]**
- **Waymo "200M+ autonomous miles"** may conflate cumulative vs rider-only figures. The ~500k paid-rides/week and durable-unit-economics claims are well-supported; the mileage number specifically is flagged. **[medium]**
- **PaXini's "~80% of humanoids use its tactile sensors"** is self-reported. **[low]**
- **Reducer/roller-screw value-share shift (~85% → ~45–55%)** is directional, not audited. **[medium]**

**Data-hygiene notes from the completeness critic:**
- Several entities appear under duplicate names (Physical Intelligence, AgiBot, RoboSense, maxon, and Harmonic Drive 3×). **Harmonic Drive SE (Germany)** and **Harmonic Drive Systems Inc (Japan)** are related-but-distinct legal entities — kept deliberately.
- **"Acrab"** (compute) and the garbled **"GSA (Groupe SA / General Screw Arts)"** name are likely data-entry artifacts; GSA is the Swiss/French planetary-roller-screw maker. **[low]**
- Product-line breakouts (Nvidia / Isaac / DRIVE; Qualcomm / Snapdragon Ride) should not be double-counted as separate companies.

**Category corrections carried into the text (do not re-introduce the originals):** Waabi = **trucking** not robotaxi · Nuro = **software/licensing** not hardware OEM · Covariant = **absorbed by Amazon**, not a live brain vendor · "AV Skyfall (Ukraine)" splits into **AeroVironment (US)** + **Auterion's separate Ukraine deal** · Amazon Robotics is **not** a purely closed vertical.

**Structural blind spots the reader should hold in mind:**
1. The map is *frontier-weighted*. The profitable industrial installed base (FANUC/ABB/Yaskawa/KUKA) and whole verticals (agriculture, construction/mining, exoskeletons, eVTOL, consumer/service, maritime) are the actual robot revenue *today* and were under-covered by both leads.
2. **Capital type changes the fragility reading.** Sovereign-underwritten "commodity" layers (FPV airframes, Chinese substitute chips) behave like durable-demand layers, not race-to-zero ones.
3. **The two spines diverge on who will save them.** The compute pinch is being funded around by hyperscalers; the actuation pinch is not funded by anyone — which is precisely why it is the more overlooked structural constraint on humanoid scale.

*Nothing above should be read as investment advice; several of the highest-valued names (Figure, Skild, Tesla, 1X, the China GPU cohort) are explicitly flagged as narrative-ahead-of-deployment.*

---

## Provenance

Generated 2026-07-13 by the `robotics-map` workflow: 9 Sonnet scouts (3 stack-layer + 6 form-factor) -> adversarial fact-check per scout -> 2 Opus synthesis leads (supply-chain/structure + capital/attention/longevity) with cross-review -> completeness critic + gap-fill -> Opus editor. 25 agents, 0 errors, ~1.29M research tokens. Structured records + supply-edge graph in `robotics_map.json` (sibling file).
