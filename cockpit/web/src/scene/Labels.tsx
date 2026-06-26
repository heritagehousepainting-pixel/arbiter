/**
 * Labels.tsx — legibility overlay for the constellation.
 *
 *  - ZoneLabels: big, dim, uppercase names that mark each layer of the pipeline
 *    (DATA SOURCES → SMART MONEY → COUNCIL → DECISION CORE → … → TRADES).
 *  - NodeLabels: always-on small labels for the KEY nodes (advisors, core parts,
 *    execution, data sources, infra, trades). Figures are NOT labeled here —
 *    there are ~75 of them; their name shows on hover via the tooltip.
 *
 * Uses drei <Html> (DOM labels) so it renders crisply offline with no font fetch.
 */
import { Html } from "@react-three/drei";
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import type { Cluster, Node, NodeType } from "../contract";
import { CLUSTER_ANCHOR } from "./layout";

const ZONE_NAME: Record<Cluster, string> = {
  sources: "DATA SOURCES",
  figures: "SMART MONEY",
  council: "THE COUNCIL",
  core: "DECISION CORE",
  ideas: "IDEAS",
  execution: "EXECUTION",
  market: "TRADES",
  learning: "LEARNING LOOP",
  infra: "INFRA",
  options: "OPTIONS",
};

const LABEL_TYPES = new Set<NodeType>([
  "advisor",
  "engine_part",
  "exec_part",
  "data_source",
  "infra",
  "trade",
]);

export function ZoneLabels({ scale = 1 }: { scale?: number }) {
  return (
    <>
      {(Object.keys(ZONE_NAME) as Cluster[]).map((c) => {
        const [x, y, z] = CLUSTER_ANCHOR[c];
        // Lift the zone name above the cluster's top so it never sits on a node
        // label. Clusters with a taller spread need more lift (council/core fan
        // their labeled nodes vertically; figures is the big bank).
        const ZONE_LIFT: Partial<Record<Cluster, number>> = {
          figures: 15, council: 15, core: 12, sources: 11, execution: 11, market: 11,
        };
        const lift = ZONE_LIFT[c] ?? 8;
        // Lever C correction: multiply entire world position (including lift) by G
        // so labels stay centered over their (now G-scaled) clusters.
        // Lever E: counter-scale distanceFactor by G so labels keep on-screen size.
        return (
          <Html
            key={c}
            position={[x * scale, (y + lift) * scale, z * scale]}
            center
            distanceFactor={46 * scale}
            zIndexRange={[0, 10]}
            style={{ pointerEvents: "none", userSelect: "none" }}
          >
            <div
              style={{
                color: c === "core" ? "#ff8fa8" : c === "options" ? "#f9a825" : "#6b7694",
                fontSize: 13,
                fontWeight: 800,
                letterSpacing: 3,
                whiteSpace: "nowrap",
                textShadow: "0 0 10px #000, 0 0 4px #000",
                opacity: 0.85,
              }}
            >
              {ZONE_NAME[c]}
            </div>
          </Html>
        );
      })}
    </>
  );
}

export function NodeLabels({
  nodes,
  positions,
  scale = 1,
}: {
  nodes: Node[];
  positions: Map<string, [number, number, number]>;
  scale?: number;
}) {
  // Refs to each label's text div + the vertical offset currently applied to it.
  const innerRefs = useRef<Map<string, HTMLDivElement>>(new Map());
  const dyRef = useRef<Map<string, number>>(new Map());
  const tick = useRef(0);

  // Vertical de-clutter: every few frames, read the REAL on-screen label boxes
  // (so it's correct under drei's distance-scaling and any orbit/zoom) and push
  // any colliding labels down so each name gets its own row. Only the text
  // offsets via translateY — node dots, edges, and motion are untouched.
  useFrame(() => {
    tick.current += 1;
    if (tick.current % 8 !== 0) return; // throttle; labels only move with the camera
    const refs = innerRefs.current;
    const dys = dyRef.current;

    // 1) read anchor boxes (subtract the offset we previously applied → true anchor)
    const boxes: { id: string; ax: number; ay: number; w: number; h: number }[] = [];
    refs.forEach((el, id) => {
      const r = el.getBoundingClientRect();
      if (r.width === 0 || r.height === 0) return;
      boxes.push({ id, ax: r.left, ay: r.top - (dys.get(id) ?? 0), w: r.width, h: r.height });
    });
    if (boxes.length < 2) return;

    // 2) greedy top-down placement: push a label below any higher one it overlaps
    boxes.sort((a, b) => a.ay - b.ay);
    const gap = 2;
    const placed: { ax: number; w: number; y: number; h: number }[] = [];
    const next = new Map<string, number>();
    for (const b of boxes) {
      let y = b.ay;
      for (let pass = 0; pass < 24; pass++) {
        let moved = false;
        for (const p of placed) {
          const hOverlap = b.ax < p.ax + p.w && p.ax < b.ax + b.w;
          const vOverlap = y < p.y + p.h + gap && p.y < y + b.h + gap;
          if (hOverlap && vOverlap) { y = p.y + p.h + gap; moved = true; }
        }
        if (!moved) break;
      }
      // Clamp the offset, and record the ACTUAL placed y (anchor + clamped dy) so
      // the collision model matches what's rendered even at the clamp extremes.
      const dy = Math.max(-48, Math.min(72, y - b.ay));
      next.set(b.id, dy);
      placed.push({ ax: b.ax, w: b.w, y: b.ay + dy, h: b.h });
    }

    // 3) apply (skip writes when unchanged to avoid needless style churn)
    next.forEach((dy, id) => {
      if (Math.abs((dys.get(id) ?? 0) - dy) > 0.5) {
        const el = refs.get(id);
        if (el) el.style.transform = `translateY(${dy.toFixed(1)}px)`;
      }
      dys.set(id, dy);
    });
  });

  return (
    <>
      {nodes
        .filter((n) => LABEL_TYPES.has(n.type))
        .map((n) => {
          const p = positions.get(n.id);
          if (!p) return null;
          const isTrade = n.type === "trade";
          return (
            <Html
              key={n.id}
              position={[p[0], p[1] + 1.4, p[2]]}
              center
              distanceFactor={(isTrade ? 22 : 28) * scale}
              zIndexRange={[0, 5]}
              style={{ pointerEvents: "none", userSelect: "none" }}
            >
              <div
                ref={(el) => {
                  if (el) {
                    innerRefs.current.set(n.id, el);
                  } else {
                    innerRefs.current.delete(n.id);
                    dyRef.current.delete(n.id);
                  }
                }}
                style={{
                  color: isTrade ? "#ffffff" : "#c8d2f0",
                  fontSize: isTrade ? 13 : 11,
                  fontWeight: isTrade ? 700 : 500,
                  whiteSpace: "nowrap",
                  textShadow: "0 0 6px #000, 0 0 2px #000",
                  willChange: "transform",
                }}
              >
                {n.label}
              </div>
            </Html>
          );
        })}
    </>
  );
}
