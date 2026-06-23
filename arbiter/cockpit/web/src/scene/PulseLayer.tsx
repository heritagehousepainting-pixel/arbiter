/**
 * PulseLayer.tsx — animated particles that travel along edges when SSE events fire.
 *
 * Mapping logic:
 *   fill / cover / opinion / outcome → the event's node_ids[] are used to find
 *   all graph edges whose source AND target appear in node_ids.  If none match,
 *   fall back to any edge that has at least one endpoint in node_ids.
 *
 *   heartbeat → pulse the infra/daemon node
 *   idea_new / idea_transition → pulse edges touching the idea node
 *   breaker / alert → pulse core cluster edges
 *
 * Each pulse is a small sphere that lerps from source to target over ~0.8s,
 * then despawns.  We keep at most MAX_PULSES alive simultaneously for perf.
 * When prefers-reduced-motion is set, pulses are disabled.
 */
import { useFrame } from "@react-three/fiber";
import { useEffect, useRef, useState } from "react";
import * as THREE from "three";
import { subscribeEvents } from "../api";
import { CLUSTER_COLOR, type CockpitEvent, type Edge, type Node } from "../contract";

const MAX_PULSES = 40;
const PULSE_DURATION = 0.9; // seconds

interface Pulse {
  id: number;
  from: [number, number, number];
  to: [number, number, number];
  color: THREE.Color;
  t: number; // 0..1 progress
}

let _nextId = 0;

function edgesForEvent(
  event: CockpitEvent,
  allEdges: Edge[],
): Edge[] {
  const ids = new Set(event.node_ids);

  // Primary: edges where BOTH endpoints match
  const both = allEdges.filter((e) => ids.has(e.source) && ids.has(e.target));
  if (both.length > 0) return both;

  // Fallback: edges where at least one endpoint matches
  return allEdges.filter((e) => ids.has(e.source) || ids.has(e.target));
}

function colorForEvent(event: CockpitEvent, nodeById: Map<string, Node>): THREE.Color {
  const c = new THREE.Color();
  switch (event.kind) {
    case "fill":      return c.set(CLUSTER_COLOR.execution);
    case "cover":     return c.set(CLUSTER_COLOR.market);
    case "opinion":   return c.set(CLUSTER_COLOR.council);
    case "outcome":   return c.set(CLUSTER_COLOR.learning);
    case "idea_new":
    case "idea_transition": return c.set(CLUSTER_COLOR.ideas);
    case "heartbeat": return c.set(CLUSTER_COLOR.infra);
    case "breaker":
    case "alert":     return c.set("#ef476f");
    default: {
      // color by the cluster of the first known node
      const firstId = event.node_ids[0];
      const n = firstId ? nodeById.get(firstId) : undefined;
      return c.set(n ? (CLUSTER_COLOR[n.cluster] ?? "#ffffff") : "#ffffff");
    }
  }
}

interface Props {
  allEdges: Edge[];
  allNodes: Node[];
  positions: Map<string, [number, number, number]>;
  reducedMotion: boolean;
}

export function PulseLayer({ allEdges, allNodes, positions, reducedMotion }: Props) {
  const [pulses, setPulses] = useState<Pulse[]>([]);
  const pulseRef = useRef<Pulse[]>([]);

  const nodeById = useRef(new Map<string, Node>());
  useEffect(() => {
    nodeById.current = new Map(allNodes.map((n) => [n.id, n]));
  }, [allNodes]);

  // Subscribe to SSE events
  useEffect(() => {
    if (reducedMotion) return;

    const unsub = subscribeEvents((event: CockpitEvent) => {
      const matchedEdges = edgesForEvent(event, allEdges);
      const color = colorForEvent(event, nodeById.current);

      const newPulses: Pulse[] = matchedEdges
        .slice(0, 4) // cap per-event
        .map((e) => {
          const from = positions.get(e.source);
          const to = positions.get(e.target);
          if (!from || !to) return null;
          return {
            id: _nextId++,
            from,
            to,
            color: color.clone(),
            t: 0,
          } satisfies Pulse;
        })
        .filter((p): p is Pulse => p !== null);

      if (newPulses.length === 0) return;

      pulseRef.current = [...pulseRef.current, ...newPulses].slice(-MAX_PULSES);
      setPulses([...pulseRef.current]);
    });

    return unsub;
  }, [allEdges, positions, reducedMotion]);

  // Animate pulses
  useFrame((_, delta) => {
    if (pulseRef.current.length === 0) return;
    let changed = false;
    const next: Pulse[] = [];
    for (const p of pulseRef.current) {
      const t = p.t + delta / PULSE_DURATION;
      if (t < 1) {
        next.push({ ...p, t });
        changed = true;
      } else {
        changed = true; // removing is also a change
      }
    }
    pulseRef.current = next;
    if (changed) setPulses([...next]);
  });

  if (reducedMotion || pulses.length === 0) return null;

  return (
    <>
      {pulses.map((p) => {
        // Lerp position
        const x = p.from[0] + (p.to[0] - p.from[0]) * p.t;
        const y = p.from[1] + (p.to[1] - p.from[1]) * p.t;
        const z = p.from[2] + (p.to[2] - p.from[2]) * p.t;
        // Fade in then out
        const opacity = p.t < 0.1 ? p.t / 0.1 : p.t > 0.85 ? (1 - p.t) / 0.15 : 1;
        const scale = 0.22 + 0.12 * Math.sin(p.t * Math.PI);
        return (
          <mesh key={p.id} position={[x, y, z]} scale={scale}>
            <sphereGeometry args={[1, 8, 8]} />
            <meshStandardMaterial
              color={p.color}
              emissive={p.color}
              emissiveIntensity={2.5 * opacity}
              transparent
              opacity={opacity * 0.95}
              depthWrite={false}
            />
          </mesh>
        );
      })}
    </>
  );
}
