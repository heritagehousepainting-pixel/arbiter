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

export function ZoneLabels() {
  return (
    <>
      {(Object.keys(ZONE_NAME) as Cluster[]).map((c) => {
        const [x, y, z] = CLUSTER_ANCHOR[c];
        const lift = c === "figures" ? 15 : c === "core" ? 8 : 8;
        return (
          <Html
            key={c}
            position={[x, y + lift, z]}
            center
            distanceFactor={46}
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
}: {
  nodes: Node[];
  positions: Map<string, [number, number, number]>;
}) {
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
              distanceFactor={isTrade ? 22 : 28}
              zIndexRange={[0, 5]}
              style={{ pointerEvents: "none", userSelect: "none" }}
            >
              <div
                style={{
                  color: isTrade ? "#ffffff" : "#c8d2f0",
                  fontSize: isTrade ? 13 : 11,
                  fontWeight: isTrade ? 700 : 500,
                  whiteSpace: "nowrap",
                  textShadow: "0 0 6px #000, 0 0 2px #000",
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
