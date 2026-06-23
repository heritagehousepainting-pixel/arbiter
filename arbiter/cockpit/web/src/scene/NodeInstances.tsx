/**
 * NodeInstances.tsx — GPU-instanced rendering for figure nodes.
 *
 * The ~79 figure nodes are the most numerous cluster.  Rather than one mesh
 * per node, we use a single InstancedMesh for the whole figures cluster so
 * the GPU sees one draw call.
 *
 * Intensity is encoded as emissiveIntensity via per-instance color.
 * Hover + selection are detected via raycasting against the InstancedMesh
 * (R3F gives us instanceId in the click event).
 */
import { useFrame } from "@react-three/fiber";
import { useRef } from "react";
import * as THREE from "three";
import { CLUSTER_COLOR, type Node, type NodeState } from "../contract";
import { useHoverStore } from "./useHoverStore";

const _dummy = new THREE.Object3D();
const _color = new THREE.Color();

interface Props {
  nodes: Node[];           // only figure-cluster nodes
  positions: Map<string, [number, number, number]>;
  nodeStates: Record<string, NodeState>;
  onSelect?: (id: string) => void;
  halted: boolean;
}

export function FigureInstances({ nodes, positions, nodeStates, onSelect, halted }: Props) {
  const meshRef = useRef<THREE.InstancedMesh>(null!);
  const setHovered = useHoverStore((s) => s.setHovered);
  const hovered = useHoverStore((s) => s.hoveredId);

  // Update instance transforms + colors every frame
  useFrame(() => {
    const mesh = meshRef.current;
    if (!mesh) return;

    for (let i = 0; i < nodes.length; i++) {
      const n = nodes[i];
      const pos = positions.get(n.id);
      if (!pos) continue;

      _dummy.position.set(...pos);
      const ns = nodeStates[n.id];
      const intensity = ns?.intensity ?? 0;
      // size encodes trust weight if present (meta.trust_weight), else uniform
      const tw = typeof n.meta?.trust_weight === "number" ? (n.meta.trust_weight as number) : 0.5;
      const scale = halted ? 0.18 : 0.18 + tw * 0.22;
      _dummy.scale.setScalar(n.id === hovered ? scale * 1.4 : scale);
      _dummy.updateMatrix();
      mesh.setMatrixAt(i, _dummy.matrix);

      // emissive punch via color brightness
      const base = _color.set(CLUSTER_COLOR.figures);
      const factor = halted ? 0.15 : 0.3 + intensity * 0.7;
      mesh.setColorAt(i, base.multiplyScalar(factor));
    }
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
  });

  if (nodes.length === 0) return null;

  return (
    <instancedMesh
      ref={meshRef}
      args={[undefined, undefined, nodes.length]}
      onClick={(e) => {
        e.stopPropagation();
        const id = nodes[e.instanceId ?? 0]?.id;
        if (id) onSelect?.(id);
      }}
      onPointerOver={(e) => {
        e.stopPropagation();
        const id = nodes[e.instanceId ?? 0]?.id;
        if (id) setHovered(id);
      }}
      onPointerOut={(e) => {
        e.stopPropagation();
        setHovered(null);
      }}
    >
      <sphereGeometry args={[1, 12, 12]} />
      <meshStandardMaterial
        color={CLUSTER_COLOR.figures}
        emissive={new THREE.Color(CLUSTER_COLOR.figures)}
        emissiveIntensity={0.6}
        roughness={0.4}
        metalness={0.3}
      />
    </instancedMesh>
  );
}
