/**
 * EdgeLines.tsx — graph edges as thin lines (one draw call per batch).
 *
 * Legibility pass:
 *  - Edges are colored by their endpoints' cluster (not a flat red).
 *  - Base opacity is LOW so the ~150 edges read as a quiet substrate, not noise.
 *  - When a node is focused (hovered/selected), only the edges TOUCHING it light
 *    up bright (additive) and the rest dim further — so you can trace one node's
 *    connections instead of a red crisscross.
 */
import { useMemo } from "react";
import * as THREE from "three";
import type { Cluster, Edge } from "../contract";
import { CLUSTER_COLOR } from "../contract";

interface Props {
  edges: Edge[];
  positions: Map<string, [number, number, number]>;
  /** node id → cluster, for per-edge color (defaults to a muted slate). */
  nodeCluster?: Map<string, Cluster>;
  /** base line opacity when nothing is focused. */
  opacity?: number;
  /** the focused node id (hovered/selected); its edges are highlighted. */
  focusId?: string | null;
}

const FALLBACK = "#3a4561";

function colorFor(id: string, nodeCluster?: Map<string, Cluster>): string {
  const c = nodeCluster?.get(id);
  return (c && CLUSTER_COLOR[c]) || FALLBACK;
}

/** Build a colored line geometry (gradient source→target color). */
function buildGeo(
  edges: Edge[],
  positions: Map<string, [number, number, number]>,
  nodeCluster: Map<string, Cluster> | undefined,
): THREE.BufferGeometry {
  const verts: number[] = [];
  const cols: number[] = [];
  const c = new THREE.Color();
  for (const e of edges) {
    const s = positions.get(e.source);
    const t = positions.get(e.target);
    if (!s || !t) continue;
    verts.push(...s, ...t);
    c.set(colorFor(e.source, nodeCluster));
    cols.push(c.r, c.g, c.b);
    c.set(colorFor(e.target, nodeCluster));
    cols.push(c.r, c.g, c.b);
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(verts, 3));
  geo.setAttribute("color", new THREE.Float32BufferAttribute(new Float32Array(cols), 3));
  return geo;
}

/** One edge layer: a quiet base + (when focused) a bright highlight overlay. */
function EdgeLayer({ edges, positions, nodeCluster, opacity = 0.12, focusId }: Props) {
  const baseGeo = useMemo(
    () => buildGeo(edges, positions, nodeCluster),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [edges.length, positions.size, nodeCluster?.size],
  );

  const focusGeo = useMemo(() => {
    if (!focusId) return null;
    const touching = edges.filter((e) => e.source === focusId || e.target === focusId);
    if (touching.length === 0) return null;
    return buildGeo(touching, positions, nodeCluster);
  }, [edges, positions, nodeCluster, focusId]);

  // Dim the base substrate further when something is focused.
  const baseOpacity = focusId ? opacity * 0.35 : opacity;

  return (
    <>
      <lineSegments geometry={baseGeo}>
        <lineBasicMaterial vertexColors transparent opacity={baseOpacity} depthWrite={false} />
      </lineSegments>
      {focusGeo && (
        <lineSegments geometry={focusGeo}>
          <lineBasicMaterial
            vertexColors
            transparent
            opacity={0.95}
            depthWrite={false}
            blending={THREE.AdditiveBlending}
          />
        </lineSegments>
      )}
    </>
  );
}

/** Static topology backbone — very quiet. */
export function StaticEdges(props: Props) {
  return <EdgeLayer {...props} opacity={props.opacity ?? 0.07} />;
}

/** Live flow edges (figure→advisor→idea→trade→outcome) — slightly brighter. */
export function DynamicEdges(props: Props) {
  return <EdgeLayer {...props} opacity={props.opacity ?? 0.16} />;
}
