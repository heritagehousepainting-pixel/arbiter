/**
 * useHoverStore — INTEGRATION ADAPTER.
 *
 * Lane 3 (scene) and Lane 4 (ui) independently created hover stores. To unify
 * them, this is now a thin adapter over the canonical `ui/store` so the scene's
 * `setHovered` writes land on the SAME state Lane 4's tooltip reads
 * (`useCockpitStore.hoveredId`). Scene call-sites are unchanged
 * (`useHoverStore((s) => s.hoveredId)` / `(s) => s.setHovered`).
 */
import { useCockpitStore } from "../ui/store";

interface HoverSlice {
  hoveredId: string | null;
  setHovered: (id: string | null) => void;
}

/** Selector hook backed by the canonical cockpit store. */
export function useHoverStore<T>(selector: (s: HoverSlice) => T): T {
  return useCockpitStore((s) =>
    selector({ hoveredId: s.hoveredId, setHovered: s.setHoveredId }),
  );
}
