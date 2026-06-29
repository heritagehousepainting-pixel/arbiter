/**
 * Personal display-only watchlist store — owned by the watchlist-charts feature.
 *
 * Kept SEPARATE from useCockpitStore (which owns transient 3D nav state) so that
 * the persist middleware never touches hoveredId / selectedId / focusCluster etc.
 *
 * Only `watchlistSymbols` is persisted to localStorage; `activeWatchSymbol` and
 * `activeChartRange` are intentionally ephemeral (reset on refresh).
 */
import { create } from "zustand";
import { persist, createJSONStorage } from "zustand/middleware";
import type { ChartRange } from "../contract";

// Personal display-only watchlist. MUST NEVER be forwarded to arbiter's ingest runner or trading engine.
export interface WatchlistStoreState {
  watchlistSymbols: string[];
  activeWatchSymbol: string | null;
  activeChartRange: ChartRange;

  addWatchlistSymbol: (sym: string) => void;
  removeWatchlistSymbol: (sym: string) => void;
  hasWatchlistSymbol: (sym: string) => boolean;
  setActiveWatchSymbol: (sym: string | null) => void;
  setActiveChartRange: (r: ChartRange) => void;
}

export const useWatchlistStore = create<WatchlistStoreState>()(
  persist(
    (set, get) => ({
      watchlistSymbols: [],
      activeWatchSymbol: null,
      activeChartRange: "1m" as ChartRange,

      addWatchlistSymbol: (sym) => {
        const upper = sym.toUpperCase();
        set((s) => {
          if (s.watchlistSymbols.includes(upper)) return s;
          return { watchlistSymbols: [...s.watchlistSymbols, upper] };
        });
      },

      removeWatchlistSymbol: (sym) => {
        const upper = sym.toUpperCase();
        set((s) => ({
          watchlistSymbols: s.watchlistSymbols.filter((x) => x !== upper),
        }));
      },

      hasWatchlistSymbol: (sym) => {
        return get().watchlistSymbols.includes(sym.toUpperCase());
      },

      setActiveWatchSymbol: (sym) => set({ activeWatchSymbol: sym }),
      setActiveChartRange: (r) => set({ activeChartRange: r }),
    }),
    {
      name: "cockpit-watchlist",
      storage: createJSONStorage(() => localStorage),
      // Only persist the symbol list — active selections reset on refresh intentionally.
      partialize: (state) => ({ watchlistSymbols: state.watchlistSymbols }),
    },
  ),
);
