/**
 * NodeMesh.tsx — individual mesh for non-figure nodes.
 *
 * Special encodings:
 *   ideas   → color by FSM state (meta.state): draft=muted, active=ideas-accent,
 *              pending=yellow, closed=dim.
 *   trades  → long = cool blue upward offset; short = warm red downward offset;
 *              size = notional / avg_price; glow intensity = |unrealized P&L|.
 *   core    → brighter, slightly larger, pulsing emissive.
 *   infra   → daemon node beacon pulses with heartbeat cadence.
 *   kill_switch halted → desaturated + contracted.
 */
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import { CLUSTER_COLOR, type Node, type NodeState } from "../contract";
import { useHoverStore } from "./useHoverStore";

// FSM state → color (ideas)
const IDEA_STATE_COLOR: Record<string, string> = {
  draft:    "#6b7280",
  active:   "#c77dff",
  pending:  "#ffd166",
  closing:  "#fb923c",
  closed:   "#374151",
};

function nodeSize(node: Node): number {
  const meta = node.meta ?? {};
  switch (node.type) {
    case "trade": {
      const notional = typeof meta.notional === "number" ? (meta.notional as number) : 0;
      return Math.max(0.35, Math.min(1.4, 0.35 + notional / 50000));
    }
    case "figure": return 0.32;
    case "advisor": return 0.72;
    case "engine_part": return 0.58;
    case "idea": return 0.48;
    case "exec_part": return 0.42;
    case "outcome": return 0.38;
    case "infra": return 0.52;
    case "data_source": return 0.55;
    default: return 0.45;
  }
}

function nodeColor(node: Node, ns: NodeState | undefined, halted: boolean): THREE.Color {
  const c = new THREE.Color();
  if (halted) {
    // Desaturate when kill switch is engaged
    const base = new THREE.Color(CLUSTER_COLOR[node.cluster] ?? "#ffffff");
    const hsl = { h: 0, s: 0, l: 0 };
    base.getHSL(hsl);
    return c.setHSL(hsl.h, hsl.s * 0.15, hsl.l * 0.4);
  }

  if (node.type === "idea") {
    const state = String(node.meta?.state ?? "draft");
    return c.set(IDEA_STATE_COLOR[state] ?? CLUSTER_COLOR.ideas);
  }

  if (node.type === "trade") {
    const side = String(node.meta?.side ?? "long");
    return c.set(side === "short" ? "#ef476f" : "#4cc9f0");
  }

  const base = CLUSTER_COLOR[node.cluster] ?? "#ffffff";
  const intensity = ns?.intensity ?? 0;
  return c.set(base).multiplyScalar(0.6 + intensity * 0.4);
}

function emissiveIntensity(node: Node, ns: NodeState | undefined, halted: boolean): number {
  if (halted) return 0.05;
  const base = ns?.intensity ?? 0;
  if (node.cluster === "core") return 0.8 + base * 1.6;
  if (node.type === "trade") {
    const upl = typeof node.meta?.unrealized_pl === "number"
      ? Math.abs(node.meta.unrealized_pl as number) : 0;
    return 0.4 + Math.min(2.0, upl / 200);
  }
  return 0.25 + base * 1.2;
}

interface Props {
  node: Node;
  position: [number, number, number];
  ns: NodeState | undefined;
  onSelect?: (id: string) => void;
  halted: boolean;
  reducedMotion: boolean;
}

export function NodeMesh({ node, position, ns, onSelect, halted, reducedMotion }: Props) {
  const meshRef = useRef<THREE.Mesh>(null!);
  const setHovered = useHoverStore((s) => s.setHovered);
  const hoveredId = useHoverStore((s) => s.hoveredId);
  const isHovered = hoveredId === node.id;

  const size = nodeSize(node);
  const color = nodeColor(node, ns, halted);
  const emissive = color.clone();
  const emissInt = emissiveIntensity(node, ns, halted);

  // Subtle Y-offset for trades: long floats up, short sinks down
  const yOffset =
    node.type === "trade"
      ? String(node.meta?.side ?? "long") === "long" ? 0.6 : -0.6
      : 0;
  const pos: [number, number, number] = [
    position[0],
    position[1] + yOffset,
    position[2],
  ];

  // Pulsing emissive for core + infra nodes (disabled on reduced-motion)
  useFrame(({ clock }) => {
    if (reducedMotion) return;
    const mesh = meshRef.current;
    if (!mesh) return;
    const mat = mesh.material as THREE.MeshStandardMaterial;
    if (node.cluster === "core") {
      mat.emissiveIntensity = emissInt + Math.sin(clock.elapsedTime * 1.8) * 0.25;
    } else if (node.type === "infra") {
      // Beacon pulse for daemon node
      mat.emissiveIntensity = emissInt + Math.sin(clock.elapsedTime * 3.5) * 0.4;
    }
  });

  const displaySize = isHovered ? size * 1.35 : size;

  return (
    <mesh
      ref={meshRef}
      position={pos}
      scale={displaySize}
      onClick={(e) => {
        e.stopPropagation();
        onSelect?.(node.id);
      }}
      onPointerOver={(e) => {
        e.stopPropagation();
        setHovered(node.id);
      }}
      onPointerOut={(e) => {
        e.stopPropagation();
        setHovered(null);
      }}
    >
      <sphereGeometry args={[1, node.cluster === "core" ? 24 : 16, 16]} />
      <meshStandardMaterial
        color={color}
        emissive={emissive}
        emissiveIntensity={emissInt}
        roughness={node.cluster === "core" ? 0.1 : 0.35}
        metalness={node.cluster === "core" ? 0.7 : 0.2}
        transparent={node.type === "outcome"}
        opacity={node.type === "outcome" ? 0.75 : 1.0}
      />
    </mesh>
  );
}
