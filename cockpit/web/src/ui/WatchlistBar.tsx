/**
 * WatchlistBar — collapsed search icon ↔ expanded ticker-search panel.
 *
 * Placement: position:absolute, top:16, right:16, zIndex:20
 * (Sits above WatchlistChartBox which is zIndex:18.)
 *
 * States:
 *   collapsed  — 32×32 magnifying-glass icon button
 *   expanded   — ~380px panel with text input + saved ticker chips
 *
 * Occlusion guards (mirror OptionsPanel pattern):
 *   • inspectionOpen prop → auto-collapse (same as OptionsPanel)
 *   • activeWatchSymbol set  → auto-collapse (ChartBox takes the space)
 *
 * Symbol validation:
 *   fetchTickerDetail(sym) → name != null → known → add immediately
 *                          → name == null → show "Unknown — add anyway?" prompt
 */
import { useEffect, useRef, useState } from "react";
import { fetchTickerDetail } from "../api";
import { motion, prefersReducedMotion } from "../theme/theme";
import { useWatchlistStore } from "./watchlistStore";

// ---------------------------------------------------------------------------
// Design tokens
// ---------------------------------------------------------------------------
const T = {
  bg: "rgba(8,10,18,0.93)",
  border: "1px solid #1c2233",
  radius: 10,
  mono: "'JetBrains Mono','Fira Code','Cascadia Code',monospace",
  sans: "'Inter','Helvetica Neue',system-ui,sans-serif",
  muted: "#8d99ae",
  text: "#e7ecff",
  accent: "#7c83ff",
  green: "#06d6a0",
  red: "#ef476f",
} as const;

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
export interface WatchlistBarProps {
  /** When true, auto-collapse to icon to avoid InspectionPanel overlap. */
  inspectionOpen: boolean;
}

// ---------------------------------------------------------------------------
// SavedTickerChip
// ---------------------------------------------------------------------------
function SavedTickerChip({
  ticker,
  isActive,
  onClick,
  onRemove,
}: {
  ticker: string;
  isActive: boolean;
  onClick: () => void;
  onRemove: () => void;
}) {
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 6px 2px 8px",
        borderRadius: 4,
        border: isActive
          ? `1px solid ${T.accent}`
          : "1px solid rgba(28,34,51,0.8)",
        background: isActive
          ? "rgba(124,131,255,0.2)"
          : "rgba(255,255,255,0.06)",
        boxShadow: isActive
          ? "0 0 8px 2px rgba(124,131,255,0.2)"
          : "0 2px 8px rgba(0,0,0,0.55)",
        fontSize: 11,
        fontFamily: T.mono,
        fontWeight: 600,
        color: isActive ? T.accent : T.text,
        letterSpacing: 0.4,
      }}
    >
      <button
        aria-label={`View chart for ${ticker}`}
        onClick={onClick}
        style={{
          background: "none",
          border: "none",
          color: "inherit",
          cursor: "pointer",
          padding: 0,
          fontFamily: "inherit",
          fontSize: "inherit",
          fontWeight: "inherit",
          letterSpacing: "inherit",
        }}
      >
        {ticker}
      </button>
      <button
        aria-label={`Remove ${ticker} from watchlist`}
        onClick={onRemove}
        style={{
          background: "none",
          border: "none",
          color: T.muted,
          cursor: "pointer",
          fontSize: 10,
          padding: "0 1px",
          lineHeight: 1,
          display: "inline-flex",
          alignItems: "center",
        }}
      >
        ×
      </button>
    </span>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function WatchlistBar({ inspectionOpen }: WatchlistBarProps) {
  const watchlistSymbols = useWatchlistStore((s) => s.watchlistSymbols);
  const activeWatchSymbol = useWatchlistStore((s) => s.activeWatchSymbol);
  const addWatchlistSymbol = useWatchlistStore((s) => s.addWatchlistSymbol);
  const removeWatchlistSymbol = useWatchlistStore((s) => s.removeWatchlistSymbol);
  const setActiveWatchSymbol = useWatchlistStore((s) => s.setActiveWatchSymbol);

  const [barOpen, setBarOpen] = useState(false);
  const [inputValue, setInputValue] = useState("");
  const [validating, setValidating] = useState(false);
  const [unknownSym, setUnknownSym] = useState<string | null>(null); // soft "add anyway"

  const inputRef = useRef<HTMLInputElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);

  // Occlusion guard #1: InspectionPanel opens → collapse
  useEffect(() => {
    if (inspectionOpen) setBarOpen(false);
  }, [inspectionOpen]);

  // Occlusion guard #2: ChartBox opens → collapse to icon
  useEffect(() => {
    if (activeWatchSymbol) setBarOpen(false);
  }, [activeWatchSymbol]);

  // Auto-focus input when expanded
  useEffect(() => {
    if (barOpen) {
      const id = setTimeout(() => inputRef.current?.focus(), 20);
      return () => clearTimeout(id);
    }
  }, [barOpen]);

  // Click-outside to collapse
  useEffect(() => {
    if (!barOpen) return;
    const handler = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        setBarOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [barOpen]);

  // Keyboard: Esc to collapse
  useEffect(() => {
    if (!barOpen) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") setBarOpen(false);
    };
    document.addEventListener("keydown", handler);
    return () => document.removeEventListener("keydown", handler);
  }, [barOpen]);

  // Validate and add a ticker symbol
  const handleSubmit = (sym: string) => {
    const upper = sym.trim().toUpperCase();
    if (!upper) return;
    setValidating(true);
    setUnknownSym(null);
    fetchTickerDetail(upper)
      .then((detail) => {
        if (detail.name != null) {
          // Known ticker — add immediately
          addWatchlistSymbol(upper);
          setInputValue("");
        } else {
          // Unknown ticker — soft "add anyway"
          setUnknownSym(upper);
        }
      })
      .catch(() => {
        // Network error — still offer soft add
        setUnknownSym(upper);
      })
      .finally(() => setValidating(false));
  };

  const handleAddAnyway = () => {
    if (unknownSym) {
      addWatchlistSymbol(unknownSym);
      setUnknownSym(null);
      setInputValue("");
    }
  };

  const handleChipClick = (ticker: string) => {
    setActiveWatchSymbol(ticker);
    // barOpen will auto-collapse via the activeWatchSymbol effect above
  };

  const rMotion = prefersReducedMotion();
  const expandTransition = rMotion
    ? undefined
    : `max-width ${motion.normal}ms ${motion.easeOut}, opacity ${motion.fast}ms ease-out`;

  return (
    <div
      ref={panelRef}
      data-testid="watchlist-bar"
      style={{
        position: "absolute",
        top: 16,
        right: 16,
        zIndex: 20,
      }}
    >
      {barOpen ? (
        /* ── Expanded panel ──────────────────────────────────────────── */
        <div
          data-testid="watchlist-bar-expanded"
          style={{
            width: 380,
            maxWidth: "calc(100vw - 96px)",
            background: T.bg,
            border: T.border,
            borderRadius: T.radius,
            fontFamily: T.sans,
            fontSize: 12,
            color: T.text,
            backdropFilter: "blur(8px)",
            overflow: "hidden",
            transition: expandTransition,
          }}
        >
          {/* Header row */}
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "0 12px",
              height: 36,
              borderBottom: T.border,
            }}
          >
            <span style={{ fontSize: 10, fontWeight: 800, letterSpacing: 1.6, color: T.muted, textTransform: "uppercase" }}>
              Watchlist
            </span>
            <button
              aria-label="Close watchlist search"
              onClick={() => setBarOpen(false)}
              style={{
                marginLeft: "auto",
                background: "none",
                border: "none",
                color: T.muted,
                cursor: "pointer",
                fontSize: 14,
                padding: "0 2px",
                lineHeight: 1,
              }}
            >
              ✕
            </button>
          </div>

          {/* Search input */}
          <div style={{ padding: "8px 12px", borderBottom: T.border }}>
            <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
              <input
                ref={inputRef}
                data-testid="watchlist-search-input"
                aria-label="Search for a ticker to add to watchlist"
                aria-autocomplete="list"
                type="text"
                placeholder="Search ticker…"
                value={inputValue}
                onChange={(e) => {
                  setInputValue(e.target.value.toUpperCase());
                  setUnknownSym(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && inputValue.trim()) {
                    handleSubmit(inputValue);
                  }
                }}
                style={{
                  flex: 1,
                  background: "rgba(255,255,255,0.04)",
                  border: T.border,
                  borderRadius: 6,
                  padding: "5px 9px",
                  color: T.text,
                  fontFamily: T.mono,
                  fontSize: 12,
                  outline: "none",
                }}
              />
              <button
                data-testid="watchlist-add-btn"
                onClick={() => inputValue.trim() && handleSubmit(inputValue)}
                disabled={validating || !inputValue.trim()}
                style={{
                  padding: "4px 10px",
                  borderRadius: 6,
                  border: `1px solid rgba(124,131,255,0.4)`,
                  background: "rgba(124,131,255,0.12)",
                  color: T.accent,
                  cursor: "pointer",
                  fontSize: 11,
                  fontFamily: T.sans,
                  fontWeight: 600,
                }}
              >
                {validating ? "…" : "Add"}
              </button>
            </div>

            {/* Validation feedback */}
            {unknownSym && (
              <div
                data-testid="watchlist-unknown-prompt"
                style={{
                  marginTop: 6,
                  display: "flex",
                  alignItems: "center",
                  gap: 8,
                  fontSize: 11,
                  color: T.muted,
                }}
              >
                <span style={{ fontStyle: "italic" }}>
                  {unknownSym}: unknown ticker —
                </span>
                <button
                  data-testid="watchlist-add-anyway"
                  onClick={handleAddAnyway}
                  style={{
                    background: "none",
                    border: "none",
                    color: T.accent,
                    cursor: "pointer",
                    fontSize: 11,
                    fontFamily: T.sans,
                    fontWeight: 600,
                    padding: 0,
                    textDecoration: "underline",
                  }}
                >
                  add anyway
                </button>
              </div>
            )}
          </div>

          {/* Saved tickers */}
          <div style={{ padding: "8px 12px" }}>
            <div
              style={{
                fontSize: 10,
                fontWeight: 800,
                letterSpacing: 1.4,
                color: T.muted,
                textTransform: "uppercase",
                marginBottom: 8,
              }}
            >
              Saved
            </div>
            {watchlistSymbols.length === 0 ? (
              <div style={{ color: T.muted, fontStyle: "italic", fontSize: 11 }}>
                No tickers saved. Search above to add.
              </div>
            ) : (
              <div
                data-testid="watchlist-chips"
                style={{ display: "flex", flexWrap: "wrap", gap: 6 }}
              >
                {watchlistSymbols.map((t) => (
                  <SavedTickerChip
                    key={t}
                    ticker={t}
                    isActive={activeWatchSymbol === t}
                    onClick={() => handleChipClick(t)}
                    onRemove={() => removeWatchlistSymbol(t)}
                  />
                ))}
              </div>
            )}
          </div>
        </div>
      ) : (
        /* ── Collapsed icon ──────────────────────────────────────────── */
        <button
          data-testid="watchlist-icon-btn"
          aria-label="Open watchlist search"
          onClick={() => setBarOpen(true)}
          style={{
            width: 32,
            height: 32,
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            background: T.bg,
            border: T.border,
            borderRadius: T.radius,
            cursor: "pointer",
            color: T.muted,
            backdropFilter: "blur(8px)",
            transition: expandTransition,
          }}
        >
          {/* Magnifying glass SVG */}
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
            aria-hidden="true"
          >
            <circle cx="7" cy="7" r="4.5" stroke="currentColor" strokeWidth="1.5" />
            <line x1="10.5" y1="10.5" x2="14" y2="14" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
          </svg>
        </button>
      )}
    </div>
  );
}
