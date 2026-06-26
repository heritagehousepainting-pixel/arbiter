/**
 * SceneRoot.tsx — the neural constellation scene.
 *
 * Owned by Lane 3 (scene & rendering).
 *
 * Architecture:
 *   - computeLayout() runs d3-force-3d once when node-id set changes
 *   - FigureInstances: GPU-instanced mesh for all ~79 figure nodes
 *   - NodeMesh: individual mesh for every other node type
 *   - StaticEdges / DynamicEdges: BufferGeometry LineSegments
 *   - PulseLayer: particles that travel edges on SSE events
 *   - useHoverStore: zustand store exposes hoveredId for Lane 4's tooltips
 *
 * Public signature (FROZEN — do not change):
 *   export function SceneRoot({ graph, state, onSelect })
 */
import { OrbitControls, Stars } from "@react-three/drei";
import { useThree } from "@react-three/fiber";
import * as THREE from "three";
import { useEffect, useMemo, useRef, useState } from "react";
import type { Cluster, Graph, Node, State } from "../contract";
import { useCockpitStore } from "../ui/store";
import { DynamicEdges, StaticEdges } from "./EdgeLines";
import { NodeLabels, ZoneLabels } from "./Labels";
import { computeLayout } from "./layout";
import { FigureInstances } from "./NodeInstances";
import { NodeMesh } from "./NodeMesh";
import { PulseLayer } from "./PulseLayer";

// Detect prefers-reduced-motion once at module load
function prefersReducedMotion(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * FitView — frame the WHOLE constellation centered in the viewport, ONCE when
 * the layout first appears (and on window-aspect change), without yanking the
 * camera while the user is exploring. Computes the node bounding box, then sets
 * the camera distance to fit both width and height for the current aspect and
 * points the controls at the center (nudged slightly so content sits a touch
 * above middle, leaving room for the bottom positions panel).
 */
function FitView({
  positions,
}: {
  positions: Map<string, [number, number, number]>;
}) {
  const camera = useThree((s) => s.camera);
  const controls = useThree((s) => s.controls) as unknown as
    | { target: { set: (x: number, y: number, z: number) => void }; update: () => void }
    | undefined;
  const width = useThree((s) => s.size.width);
  const height = useThree((s) => s.size.height);
  const fittedFor = useRef<string>("");

  useEffect(() => {
    if (positions.size === 0 || !controls || width < 10 || height < 10) return;
    // Re-fit on a real canvas-size change or when the node set changes — NOT on
    // every /state poll (preserve manual orbit/zoom).
    const key = `${positions.size}|${width}x${height}`;
    if (fittedFor.current === key) return;

    let minX = Infinity, minY = Infinity, minZ = Infinity;
    let maxX = -Infinity, maxY = -Infinity, maxZ = -Infinity;
    for (const [x, y, z] of positions.values()) {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
      minZ = Math.min(minZ, z); maxZ = Math.max(maxZ, z);
    }
    if (!Number.isFinite(minX) || !Number.isFinite(maxX)) return;
    fittedFor.current = key;

    const cx = (minX + maxX) / 2;
    const cy = (minY + maxY) / 2;
    const cz = (minZ + maxZ) / 2;
    const sizeX = maxX - minX;
    const sizeY = maxY - minY;

    // Clamp aspect so a transient/odd canvas measurement can't blow up distance.
    const aspect = Math.min(2.6, Math.max(0.6, width / height));
    const persp = camera as unknown as { fov: number; updateProjectionMatrix: () => void };
    const vfov = (persp.fov * Math.PI) / 180;
    const fitH = sizeY / 2 / Math.tan(vfov / 2);
    const fitW = sizeX / 2 / (Math.tan(vfov / 2) * aspect);
    const dist = Math.min(195, Math.max(fitH, fitW) * 1.22 + 8);

    // Bias the framing slightly DOWN-screen (aim a bit above the bbox center) so
    // the top-center Open Positions panel doesn't cover the constellation core.
    const yShift = sizeY * 0.08;
    camera.position.set(cx, cy + yShift, cz + dist);
    controls.target.set(cx, cy + yShift, cz);
    persp.updateProjectionMatrix();
    controls.update();
  }, [positions, controls, camera, width, height]);

  return null;
}

export function SceneRoot({
  graph,
  state,
  onSelect,
}: {
  graph: Graph;
  state: State | null;
  onSelect?: (id: string) => void;
}) {
  const reducedMotion = useRef(prefersReducedMotion()).current;
  const halted = state?.kill_switch?.halted ?? false;

  // ── All nodes (static + dynamic) ────────────────────────────────────────
  const allNodes: Node[] = useMemo(
    () => [...graph.nodes, ...(state?.dynamic_nodes ?? [])],
    // Recompute when the count changes; avoids reshuffling on every /state poll
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [graph.nodes, (state?.dynamic_nodes ?? []).length],
  );

  const allEdges = useMemo(
    () => [...graph.edges, ...(state?.dynamic_edges ?? [])],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [graph.edges.length, (state?.dynamic_edges ?? []).length],
  );

  // ── Layout (stable positions) ───────────────────────────────────────────
  // posMap holds the stable position map.  We re-run layout only when new
  // node IDs appear; existing IDs keep their positions.
  const [posMap, setPosMap] = useState<Map<string, [number, number, number]>>(
    () => new Map(),
  );

  // Track which node IDs we have already laid out
  const laidOutIds = useRef(new Set<string>());

  // layoutG is the global zone scale (computed with posMap, synced to state after)
  const layoutGRef = useRef<number>(1);
  const [layoutG, setLayoutG] = useState<number>(1);

  useEffect(() => {
    const currentIds = new Set(allNodes.map((n) => n.id));
    const hasNew = [...currentIds].some((id) => !laidOutIds.current.has(id));
    if (!hasNew) return;

    setPosMap((prev) => {
      // Full run on first load; warm-start (fewer ticks) for incremental growth
      const ticks = prev.size === 0 ? 200 : 80;
      const { positions, G: newG } = computeLayout(allNodes, allEdges, {
        initial: prev.size > 0 ? prev : undefined,
        ticks,
      });
      layoutGRef.current = newG;
      return positions;
    });
    // Mark these IDs as laid out OUTSIDE the updater (updaters can run twice in
    // concurrent mode); this effect body runs once per dependency change.
    laidOutIds.current = currentIds;
  }, [allNodes, allEdges]);

  // Sync G ref to state whenever posMap changes (G and posMap always computed together)
  useEffect(() => {
    setLayoutG(layoutGRef.current);
  }, [posMap]);

  // ── Split nodes by rendering strategy ───────────────────────────────────
  const figureNodes = useMemo(
    () => allNodes.filter((n) => n.cluster === "figures"),
    [allNodes],
  );
  const otherNodes = useMemo(
    () => allNodes.filter((n) => n.cluster !== "figures"),
    [allNodes],
  );

  const nodeStates = state?.nodes ?? {};

  // Camera framing target — the labeled "spine" (data sources → council → core →
  // execution → trades). The big UNLABELED banks (figures, ideas) are excluded so
  // the camera frames the readable flow up close and the banks bleed toward the
  // edges (orbit/zoom to see them all). This matches the loved close view.
  // Exclude the LEFT-side banks (figures + their data sources) and the big ideas
  // bank from the framing box: those are the horizontal extremes, so dropping them
  // lets the camera zoom into the council→core→execution→trades flow while the
  // figure bank bleeds off the left edge.
  const SPINE_EXCLUDE = useMemo(() => new Set<Cluster>(["figures", "ideas", "sources"]), []);
  const framePositions = useMemo(() => {
    const m = new Map<string, [number, number, number]>();
    for (const n of allNodes) {
      if (SPINE_EXCLUDE.has(n.cluster)) continue;
      const p = posMap.get(n.id);
      if (p) m.set(n.id, p);
    }
    // Fallback to the full map before the first layout settles.
    return m.size > 0 ? m : posMap;
  }, [allNodes, posMap, SPINE_EXCLUDE]);

  // id → cluster, so edges can be colored by their endpoints (not a flat red).
  const nodeCluster = useMemo(() => {
    const m = new Map<string, Cluster>();
    for (const n of allNodes) m.set(n.id, n.cluster);
    return m;
  }, [allNodes]);

  // The focused node (hover wins over selection) drives edge highlighting so you
  // can trace ONE node's connections instead of the whole crisscross.
  const hoveredId = useCockpitStore((s) => s.hoveredId);
  const selectedId = useCockpitStore((s) => s.selectedId);
  const focusId = hoveredId ?? selectedId ?? null;

  // ── Static edges (graph topology) ───────────────────────────────────────
  const staticEdges = graph.edges;
  const dynamicEdges = state?.dynamic_edges ?? [];

  return (
    <>
      {/* Ambient starfield background */}
      <Stars
        radius={120}
        depth={60}
        count={reducedMotion ? 1000 : 3500}
        factor={3}
        saturation={0.2}
        fade
        speed={reducedMotion ? 0 : 0.4}
      />

      {/* Lighting */}
      <ambientLight intensity={0.25} />
      {/* Central core glow — kept LOCAL (short distance) so it doesn't tint the
          whole scene red; the bloom pass does the visible glowing. */}
      <pointLight position={[0, 0, 0]} intensity={halted ? 0.6 : 2.4} distance={22} color="#ff6b88" />
      <pointLight position={[0, 0, 8]} intensity={0.6} distance={40} color="#aab4ff" />
      {/* Soft fills per major region */}
      <pointLight position={[-30, 2, 0]}  intensity={0.8} distance={34} color="#ffd166" />
      <pointLight position={[28, 3, -3]}  intensity={0.7} distance={30} color="#4cc9f0" />
      <pointLight position={[2, 20, 5]}   intensity={0.5} distance={22} color="#80ed99" />

      {/* Static graph edges (topology) — quiet substrate */}
      {posMap.size > 0 && (
        <StaticEdges
          edges={staticEdges}
          positions={posMap}
          nodeCluster={nodeCluster}
          focusId={focusId}
          opacity={halted ? 0.04 : 0.07}
        />
      )}

      {/* Dynamic live edges (live idea flow + trades) — cluster-colored, low */}
      {posMap.size > 0 && dynamicEdges.length > 0 && (
        <DynamicEdges
          edges={dynamicEdges}
          positions={posMap}
          nodeCluster={nodeCluster}
          focusId={focusId}
          opacity={halted ? 0.08 : 0.16}
        />
      )}

      {/* Zone + key-node labels (legibility) */}
      <ZoneLabels scale={layoutG} />
      {posMap.size > 0 && <NodeLabels nodes={allNodes} positions={posMap} scale={layoutG} />}

      {/* Figure nodes — GPU instanced for perf */}
      {posMap.size > 0 && (
        <FigureInstances
          nodes={figureNodes}
          positions={posMap}
          nodeStates={nodeStates}
          onSelect={onSelect}
          halted={halted}
        />
      )}

      {/* All other node types — individual meshes */}
      {posMap.size > 0 &&
        otherNodes.map((n) => {
          const pos = posMap.get(n.id);
          if (!pos) return null;
          return (
            <NodeMesh
              key={n.id}
              node={n}
              position={pos}
              ns={nodeStates[n.id]}
              onSelect={onSelect}
              halted={halted}
              reducedMotion={reducedMotion}
            />
          );
        })}

      {/* SSE event pulses along edges */}
      {posMap.size > 0 && (
        <PulseLayer
          allEdges={allEdges}
          allNodes={allNodes}
          positions={posMap}
          reducedMotion={reducedMotion}
        />
      )}

      {/* Camera controls.
          - LEFT click-drag  = orbit (rotate) — the motion you already like.
          - RIGHT click-drag = PAN ("pull"/slide the view across) — screenSpacePanning
            makes it slide flat across your screen instead of along the tilted plane.
          - scroll / pinch   = zoom.
          - trackpad: two-finger drag also pans+zooms (DOLLY_PAN). */}
      <OrbitControls
        makeDefault
        enableDamping
        dampingFactor={0.08}
        minDistance={8}
        maxDistance={200}
        autoRotate={false}
        enablePan
        screenSpacePanning
        panSpeed={1.1}
        mouseButtons={{
          LEFT: THREE.MOUSE.ROTATE,
          MIDDLE: THREE.MOUSE.DOLLY,
          RIGHT: THREE.MOUSE.PAN,
        }}
        touches={{ ONE: THREE.TOUCH.ROTATE, TWO: THREE.TOUCH.DOLLY_PAN }}
      />

      {/* Frame the whole constellation centered, once (preserves manual orbit). */}
      {posMap.size > 0 && <FitView positions={framePositions} />}
    </>
  );
}
