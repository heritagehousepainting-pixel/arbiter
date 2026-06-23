/**
 * Cockpit shared lightweight store — owned by Lane 4.
 *
 * SEAM DOCUMENTATION (for other lanes):
 *
 * Lane 3 (scene / rendering) — drives hover and selection from 3D pointer events:
 *   import { useCockpitStore } from "../ui/store";
 *   const setHoveredId = useCockpitStore((s) => s.setHoveredId);
 *   const setSelectedId = useCockpitStore((s) => s.setSelectedId);
 *   // on pointer-over mesh: setHoveredId(node.id)
 *   // on pointer-out:       setHoveredId(null)
 *   // on click:             setSelectedId(node.id)
 *
 * Lane 5 (polish / choreography) — can read focusCluster to dim other clusters:
 *   import { useCockpitStore } from "../ui/store";
 *   const focusCluster = useCockpitStore((s) => s.focusCluster);
 *
 * App.tsx / CockpitUI — still receives selectedId as a prop (the canonical flow),
 * but CockpitUI also subscribes to the store so Lane 3 can drive selection directly
 * without re-routing through App state if needed.
 */
import { create } from "zustand";
import type { Cluster } from "../contract";

export interface CockpitStoreState {
  /** The hovered node id — set by Lane 3 on pointer-over, cleared on pointer-out. */
  hoveredId: string | null;
  /** The selected node id — set by Lane 3 on click or by the walkthrough stepper. */
  selectedId: string | null;
  /** The currently focused cluster for camera easing + dimming. */
  focusCluster: Cluster | null;
  /** 0-based index into WALKTHROUGH_PATH; null = walkthrough inactive. */
  walkthroughStep: number | null;

  setHoveredId: (id: string | null) => void;
  setSelectedId: (id: string | null) => void;
  setFocusCluster: (cluster: Cluster | null) => void;
  setWalkthroughStep: (step: number | null) => void;
}

export const useCockpitStore = create<CockpitStoreState>((set) => ({
  hoveredId: null,
  selectedId: null,
  focusCluster: null,
  walkthroughStep: null,

  setHoveredId: (id) => set({ hoveredId: id }),
  setSelectedId: (id) => set({ selectedId: id }),
  setFocusCluster: (cluster) => set({ focusCluster: cluster }),
  setWalkthroughStep: (step) => set({ walkthroughStep: step }),
}));
