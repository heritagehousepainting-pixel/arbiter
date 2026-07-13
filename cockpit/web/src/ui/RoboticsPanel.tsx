/**
 * RoboticsPanel — collapsed 🤖 icon ↔ expanded, layer-grouped robotics board.
 *
 * DISPLAY-ONLY. Reads the curated roster from GET /robotics-watchlist and shows
 * it grouped by stack layer (compute → brain → components → integrator →
 * deployment). Priceable rows open the existing WatchlistChartBox via the shared
 * watchlist store (setActiveWatchSymbol); reference rows (foreign-listed /
 * private chokepoints) render tagged but without a chart. Early-insight (★) rows
 * surface their "trigger to watch".
 *
 * Placement: position:absolute, top:16, right:56 — sits just left of WatchlistBar
 * (right:16). Mirrors WatchlistBar's occlusion guards: inspectionOpen or an active
 * chart symbol auto-collapses to the icon.
 */
import { useEffect, useRef, useState } from "react";
import { fetchRoboticsSignals, fetchRoboticsWatchlist } from "../api";
import type {
  RoboticsLayer,
  RoboticsLongevity,
  RoboticsRosterEntry,
  RoboticsSignal,
} from "../contract";
import { motion, prefersReducedMotion } from "../theme/theme";
import { useWatchlistStore } from "./watchlistStore";

// ---------------------------------------------------------------------------
// Design tokens (shared with WatchlistBar)
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
} as const;

const LAYER_ORDER: readonly RoboticsLayer[] = [
  "compute", "brain", "components", "integrator", "deployment",
];

const LONGEVITY_COLOR: Record<RoboticsLongevity, string> = {
  chokepoint: "#ffd166",   // gold — hard-to-replace bottleneck
  durable: "#06d6a0",      // green — durable winner
  commodity: "#8d99ae",    // muted — commoditized
  "hype-risk": "#ef476f",  // red — story ahead of shipped
  unclear: "#7c83ff",      // accent — unproven / policy-driven
};

// ---------------------------------------------------------------------------
// Props
// ---------------------------------------------------------------------------
export interface RoboticsPanelProps {
  /** When true, auto-collapse to icon to avoid InspectionPanel overlap. */
  inspectionOpen: boolean;
}

// ---------------------------------------------------------------------------
// Row
// ---------------------------------------------------------------------------
function RosterRow({ entry }: { entry: RoboticsRosterEntry }) {
  const setActiveWatchSymbol = useWatchlistStore((s) => s.setActiveWatchSymbol);
  return (
    <div
      data-testid={`robotics-row-${entry.symbol}`}
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        padding: "5px 4px",
        borderBottom: "1px solid rgba(28,34,51,0.5)",
      }}
    >
      <div style={{ display: "flex", alignItems: "baseline", gap: 8 }}>
        {entry.early_insight && (
          <span title="early-insight watchlist" style={{ color: "#ffd166", fontSize: 11 }}>★</span>
        )}
        {entry.priceable ? (
          <button
            aria-label={`View chart for ${entry.symbol}`}
            onClick={() => setActiveWatchSymbol(entry.symbol)}
            style={{
              background: "none",
              border: "none",
              padding: 0,
              cursor: "pointer",
              color: T.accent,
              fontFamily: T.mono,
              fontSize: 12,
              fontWeight: 700,
              letterSpacing: 0.4,
            }}
          >
            {entry.symbol}
          </button>
        ) : (
          <span
            title="reference row — no live price (foreign-listed or private)"
            style={{ color: T.muted, fontFamily: T.mono, fontSize: 12, fontWeight: 700, letterSpacing: 0.4 }}
          >
            {entry.symbol}
          </span>
        )}
        <span style={{ color: T.text, fontSize: 11, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {entry.company}
        </span>
        <span
          style={{
            fontSize: 9,
            fontWeight: 700,
            letterSpacing: 0.4,
            textTransform: "uppercase",
            color: LONGEVITY_COLOR[entry.longevity],
            border: `1px solid ${LONGEVITY_COLOR[entry.longevity]}55`,
            borderRadius: 4,
            padding: "1px 5px",
            whiteSpace: "nowrap",
          }}
        >
          {entry.longevity}
        </span>
      </div>
      {entry.early_insight && entry.trigger && (
        <div style={{ color: T.muted, fontSize: 10, fontStyle: "italic", paddingLeft: 16 }}>
          {entry.trigger}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function RoboticsPanel({ inspectionOpen }: RoboticsPanelProps) {
  const activeWatchSymbol = useWatchlistStore((s) => s.activeWatchSymbol);

  const [open, setOpen] = useState(false);
  const [entries, setEntries] = useState<RoboticsRosterEntry[] | null>(null);
  const [signals, setSignals] = useState<RoboticsSignal[] | null>(null);
  const [error, setError] = useState(false);
  const panelRef = useRef<HTMLDivElement>(null);

  // Occlusion guards (mirror WatchlistBar)
  useEffect(() => { if (inspectionOpen) setOpen(false); }, [inspectionOpen]);
  useEffect(() => { if (activeWatchSymbol) setOpen(false); }, [activeWatchSymbol]);

  // Lazy-load the roster + recent signals once, on first expand
  useEffect(() => {
    if (!open || entries !== null) return;
    let cancelled = false;
    fetchRoboticsWatchlist()
      .then((wl) => { if (!cancelled) setEntries(wl.entries); })
      .catch(() => { if (!cancelled) { setEntries([]); setError(true); } });
    fetchRoboticsSignals()
      .then((s) => { if (!cancelled) setSignals(s.signals); })
      .catch(() => { if (!cancelled) setSignals([]); });
    return () => { cancelled = true; };
  }, [open, entries]);

  // Click-outside + Esc to collapse
  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const rMotion = prefersReducedMotion();
  const transition = rMotion ? undefined : `opacity ${motion.fast}ms ease-out`;

  const groups = LAYER_ORDER
    .map((layer) => ({ layer, rows: (entries ?? []).filter((e) => e.layer === layer) }))
    .filter((g) => g.rows.length > 0);

  return (
    <div
      ref={panelRef}
      data-testid="robotics-panel"
      style={{ position: "absolute", top: 16, right: 56, zIndex: 20 }}
    >
      {open ? (
        <div
          data-testid="robotics-panel-expanded"
          style={{
            width: 420,
            maxWidth: "calc(100vw - 96px)",
            maxHeight: "calc(100vh - 48px)",
            display: "flex",
            flexDirection: "column",
            background: T.bg,
            border: T.border,
            borderRadius: T.radius,
            fontFamily: T.sans,
            color: T.text,
            backdropFilter: "blur(8px)",
            overflow: "hidden",
            transition,
          }}
        >
          {/* Header */}
          <div style={{ display: "flex", alignItems: "center", gap: 8, padding: "0 12px", height: 36, borderBottom: T.border }}>
            <span style={{ fontSize: 10, fontWeight: 800, letterSpacing: 1.6, color: T.muted, textTransform: "uppercase" }}>
              Robotics
            </span>
            <span style={{ fontSize: 10, color: T.muted }}>· picks &amp; shovels of the robot economy</span>
            <button
              aria-label="Close robotics board"
              onClick={() => setOpen(false)}
              style={{ marginLeft: "auto", background: "none", border: "none", color: T.muted, cursor: "pointer", fontSize: 14, padding: "0 2px", lineHeight: 1 }}
            >
              ✕
            </button>
          </div>

          {/* Body */}
          <div style={{ overflowY: "auto", padding: "4px 12px 10px" }}>
            {signals && signals.length > 0 && (
              <div data-testid="robotics-signals" style={{ marginTop: 8, marginBottom: 4 }}>
                <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: 1.4, color: "#ffd166", marginBottom: 2 }}>
                  RECENT SIGNALS
                </div>
                {signals.slice(0, 6).map((s, i) => (
                  <div
                    key={i}
                    data-testid="robotics-signal"
                    style={{ fontSize: 11, color: T.text, padding: "2px 0", borderBottom: "1px solid rgba(28,34,51,0.5)" }}
                  >
                    {s.trigger_hit && <span style={{ color: "#ffd166" }}>★ </span>}
                    <span>{s.headline}</span>
                    {s.trigger_name && <span style={{ color: T.muted }}> ({s.trigger_name})</span>}
                  </div>
                ))}
              </div>
            )}
            {entries === null ? (
              <div style={{ color: T.muted, fontStyle: "italic", fontSize: 11, padding: "10px 0" }}>Loading…</div>
            ) : error ? (
              <div style={{ color: T.muted, fontStyle: "italic", fontSize: 11, padding: "10px 0" }}>
                Roster unavailable.
              </div>
            ) : (
              groups.map((g) => (
                <div key={g.layer} style={{ marginTop: 8 }}>
                  <div style={{ fontSize: 10, fontWeight: 800, letterSpacing: 1.4, color: T.accent, marginBottom: 2 }}>
                    {g.layer.toUpperCase()}
                  </div>
                  {g.rows.map((e) => (
                    <RosterRow key={e.symbol} entry={e} />
                  ))}
                </div>
              ))
            )}
            <div style={{ marginTop: 10, fontSize: 9, color: T.muted, fontStyle: "italic" }}>
              ★ = early-insight watchlist · muted symbols = reference rows (no live price)
            </div>
          </div>
        </div>
      ) : (
        <button
          data-testid="robotics-icon-btn"
          aria-label="Open robotics board"
          onClick={() => setOpen(true)}
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
            fontSize: 16,
            lineHeight: 1,
            backdropFilter: "blur(8px)",
            transition,
          }}
        >
          <span aria-hidden="true">🤖</span>
        </button>
      )}
    </div>
  );
}
