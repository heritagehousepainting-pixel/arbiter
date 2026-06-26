/**
 * 2D overlay layer — owned entirely by Lane 4.
 * Contains: HUD, Legend, Inspection Panel, Hover Tooltip, Guided Walkthrough.
 *
 * Public signature (FROZEN — App.tsx depends on it):
 *   export function CockpitUI({ state, selectedId, onClose }: {
 *     state: State | null;
 *     selectedId: string | null;
 *     onClose: () => void;
 *   })
 */
import { useEffect, useMemo, useRef, useState } from "react";
import { fetchNode } from "../api";
import {
  CLUSTER_COLOR,
  type Cluster,
  type Graph,
  type NodeDetail,
  type NodeType,
  type State,
} from "../contract";
import { theme } from "../theme/theme";
import { OptionsPanel } from "./OptionsPanel";
import { PositionsPanel } from "./PositionsPanel";
import { useCockpitStore } from "./store";

// ---------------------------------------------------------------------------
// Design tokens (layer on top of theme)
// ---------------------------------------------------------------------------
const T = {
  panelBg: "rgba(8,10,18,0.93)",
  panelBorder: "1px solid #1c2233",
  radius: 10,
  fontMono: "'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace",
  fontSans: "'Inter', 'Helvetica Neue', system-ui, sans-serif",
  green: theme.ok,
  red: theme.bad,
  muted: theme.muted,
  text: theme.text,
  accent: "#7c83ff",
} as const;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function fmt(v: number | null | undefined, prefix = "", digits = 2): string {
  if (v == null) return "—";
  const s = v.toFixed(digits);
  return `${prefix}${s}`;
}

function fmtPL(v: number | null | undefined): { text: string; color: string } {
  if (v == null) return { text: "—", color: T.muted };
  const color = v >= 0 ? T.green : T.red;
  const text = `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
  return { text, color };
}

function Dot({ ok }: { ok?: boolean | null }) {
  const color = ok == null ? T.muted : ok ? T.green : T.red;
  return (
    <span
      style={{ color, fontSize: 11, marginRight: 2 }}
      aria-label={ok ? "online" : "offline"}
    >
      ●
    </span>
  );
}

function Badge({
  label,
  color,
  bg,
}: {
  label: string;
  color?: string;
  bg?: string;
}) {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px 7px",
        borderRadius: 4,
        fontSize: 11,
        fontWeight: 600,
        background: bg ?? "rgba(255,255,255,0.08)",
        color: color ?? T.text,
        letterSpacing: 0.4,
        textTransform: "uppercase" as const,
      }}
    >
      {label}
    </span>
  );
}

function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        letterSpacing: 1.4,
        color: T.muted,
        textTransform: "uppercase" as const,
        marginTop: 16,
        marginBottom: 6,
        borderBottom: "1px solid #1c2233",
        paddingBottom: 4,
      }}
    >
      {children}
    </div>
  );
}

function KV({
  k,
  v,
  vColor,
}: {
  k: string;
  v: string | React.ReactNode;
  vColor?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        gap: 8,
        marginBottom: 3,
      }}
    >
      <span style={{ color: T.muted, flexShrink: 0 }}>{k}</span>
      <span
        style={{
          color: vColor ?? T.text,
          textAlign: "right" as const,
          wordBreak: "break-all",
        }}
      >
        {v}
      </span>
    </div>
  );
}

function MiniTable({
  rows,
  cols,
}: {
  rows: Record<string, unknown>[];
  cols: { key: string; label: string; render?: (v: unknown) => string }[];
}) {
  if (!rows.length) {
    return (
      <div style={{ color: T.muted, fontStyle: "italic", fontSize: 12 }}>
        building…
      </div>
    );
  }
  return (
    <div style={{ overflowX: "auto" as const }}>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: 11,
          fontFamily: T.fontMono,
        }}
      >
        <thead>
          <tr>
            {cols.map((c) => (
              <th
                key={c.key}
                style={{
                  textAlign: "left" as const,
                  color: T.muted,
                  padding: "2px 6px 4px 0",
                  fontWeight: 600,
                  letterSpacing: 0.5,
                }}
              >
                {c.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderTop: "1px solid #1c2233" }}>
              {cols.map((c) => {
                const raw = row[c.key];
                const text = c.render ? c.render(raw) : String(raw ?? "—");
                return (
                  <td
                    key={c.key}
                    style={{ padding: "3px 6px 3px 0", color: T.text }}
                  >
                    {text}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Per-type detail sections
// ---------------------------------------------------------------------------

function FigureDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const source = String(s.source ?? "");
  const score = s.track_record_score;

  const filings = detail.rows.filter(
    (r) => (r as Record<string, unknown>).filing_ts != null
  );

  return (
    <>
      {source && (
        <KV
          k="source"
          v={<Badge label={source} color={T.accent} />}
        />
      )}
      <SectionTitle>Track Record</SectionTitle>
      {score != null ? (
        <KV k="score" v={fmt(score as number, "", 3)} />
      ) : (
        <div style={{ color: T.muted, fontStyle: "italic", fontSize: 12 }}>
          building…
        </div>
      )}

      <SectionTitle>Recent Filings</SectionTitle>
      <MiniTable
        rows={filings}
        cols={[
          { key: "ticker", label: "Ticker" },
          { key: "txn_type", label: "Type" },
          {
            key: "shares",
            render: (v) => fmt(v as number, "", 0),
            label: "Shares",
          },
          {
            key: "price",
            render: (v) => fmt(v as number, "$"),
            label: "Price",
          },
          {
            key: "filing_ts",
            render: (v) => String(v ?? "—").slice(0, 10),
            label: "Filed",
          },
        ]}
      />
    </>
  );
}

function AdvisorDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const trust = s.trust_weight;
  const winRate = s.win_rate;

  const opinions = detail.rows.filter(
    (r) => (r as Record<string, unknown>).stance_score != null
  );

  return (
    <>
      <SectionTitle>Trust</SectionTitle>
      {trust != null ? (
        <KV k="weight" v={fmt(trust as number, "", 4)} />
      ) : (
        <div style={{ color: T.muted, fontStyle: "italic", fontSize: 12 }}>
          building…
        </div>
      )}
      {winRate != null && (
        <KV k="win rate" v={fmt((winRate as number) * 100, "", 1) + "%"} />
      )}

      <SectionTitle>Recent Opinions</SectionTitle>
      <MiniTable
        rows={opinions}
        cols={[
          { key: "idea_id", label: "Idea" },
          {
            key: "stance_score",
            render: (v) => fmt(v as number, "", 3),
            label: "Stance",
          },
          {
            key: "confidence",
            render: (v) => fmt(v as number, "", 3),
            label: "Conf",
          },
          {
            key: "created_at",
            render: (v) => String(v ?? "—").slice(0, 10),
            label: "Date",
          },
        ]}
      />
    </>
  );
}

function IdeaDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;

  const stateVal = String(s.state ?? "");
  const stateColor: Record<string, string> = {
    pending: T.muted,
    approved: T.green,
    rejected: T.red,
    open: T.accent,
    closed: T.muted,
  };

  const opinions = detail.rows.filter(
    (r) =>
      (r as Record<string, unknown>).stance_score != null &&
      (r as Record<string, unknown>).advisor_id != null
  );
  const orders = detail.rows.filter(
    (r) =>
      (r as Record<string, unknown>).side != null &&
      (r as Record<string, unknown>).qty != null
  );

  return (
    <>
      <SectionTitle>Thesis</SectionTitle>
      <div
        style={{
          color: T.text,
          fontSize: 12,
          lineHeight: 1.6,
          whiteSpace: "pre-wrap",
        }}
      >
        {String(s.thesis ?? "—")}
      </div>

      <SectionTitle>State</SectionTitle>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <Badge
          label={stateVal || "unknown"}
          color={stateColor[stateVal] ?? T.muted}
        />
        {s.horizon != null && (
          <span style={{ color: T.muted, fontSize: 11 }}>
            horizon: {String(s.horizon)}
          </span>
        )}
      </div>

      <SectionTitle>Council Opinions</SectionTitle>
      <MiniTable
        rows={opinions}
        cols={[
          { key: "advisor_id", label: "Advisor" },
          {
            key: "stance_score",
            render: (v) => fmt(v as number, "", 3),
            label: "Stance",
          },
          {
            key: "confidence",
            render: (v) => fmt(v as number, "", 3),
            label: "Conf",
          },
        ]}
      />

      <SectionTitle>Orders</SectionTitle>
      <MiniTable
        rows={orders}
        cols={[
          { key: "side", label: "Side" },
          {
            key: "qty",
            render: (v) => fmt(v as number, "", 0),
            label: "Qty",
          },
          { key: "status", label: "Status" },
        ]}
      />

      {s.outcome_alpha_bps != null && (
        <>
          <SectionTitle>Outcome</SectionTitle>
          <KV k="alpha (bps)" v={fmt(s.outcome_alpha_bps as number, "", 1)} />
        </>
      )}
    </>
  );
}

function TradeDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const pl = fmtPL(s.unrealized_pl as number | null);

  return (
    <>
      <SectionTitle>Position</SectionTitle>
      <KV k="ticker" v={String(s.ticker ?? "—")} />
      <KV k="side" v={String(s.side ?? "—")} />
      <KV k="shares" v={fmt(s.qty as number, "", 0)} />
      <KV k="avg entry" v={fmt(s.avg_entry as number, "$")} />
      <KV k="unrealized P&L" v={pl.text} vColor={pl.color} />

      {s.originating_idea && (
        <>
          <SectionTitle>Origin</SectionTitle>
          <KV k="idea" v={String(s.originating_idea)} />
          {s.originating_figure && (
            <KV k="figure" v={String(s.originating_figure)} />
          )}
        </>
      )}
    </>
  );
}

function OutcomeDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const alphaBps = s.alpha_bps as number | null | undefined;
  const alphaColor =
    alphaBps == null ? T.muted : alphaBps >= 0 ? T.green : T.red;

  return (
    <>
      <SectionTitle>Result</SectionTitle>
      <KV
        k="alpha (bps)"
        v={fmt(alphaBps, "", 1)}
        vColor={alphaColor}
      />
      <KV k="binary" v={String(s.binary ?? "—")} />
      <KV k="advisor" v={String(s.advisor_id ?? "—")} />
      <KV k="label kind" v={String(s.label_kind ?? "—")} />
    </>
  );
}

function GenericDetail({ detail }: { detail: NodeDetail }) {
  // Render summary key-values + rows for any type we don't have a custom view for
  const summaryEntries = Object.entries(detail.summary);
  return (
    <>
      {summaryEntries.length > 0 && (
        <>
          <SectionTitle>Details</SectionTitle>
          {summaryEntries.map(([k, v]) => (
            <KV key={k} k={k} v={String(v ?? "—")} />
          ))}
        </>
      )}
      {detail.rows.length > 0 && (
        <>
          <SectionTitle>Data</SectionTitle>
          <div
            style={{
              fontSize: 11,
              fontFamily: T.fontMono,
              color: T.muted,
              whiteSpace: "pre-wrap",
              maxHeight: 200,
              overflowY: "auto" as const,
            }}
          >
            {JSON.stringify(detail.rows, null, 2)}
          </div>
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// opt.layer node detail
// ---------------------------------------------------------------------------
function OptLayerDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const mode = String(s.options_mode ?? s.mode ?? "—");
  const nOpen = s.n_open_positions ?? s.n_open ?? null;
  const sleeveUsed = s.sleeve_used_pct ?? null;
  const modeColor =
    mode === "paper" ? "#f9a825" : mode === "shadow" ? T.muted : T.muted;

  return (
    <>
      <SectionTitle>Options Layer</SectionTitle>
      <KV k="mode" v={<span style={{ color: modeColor, fontWeight: 600, textTransform: "uppercase" as const }}>{mode}</span>} />
      {nOpen != null && <KV k="open positions" v={String(nOpen)} />}
      {sleeveUsed != null && (
        <KV k="sleeve used" v={`${(Number(sleeveUsed) * 100).toFixed(0)}%`} />
      )}
      {detail.rows.length > 0 && (
        <>
          <SectionTitle>Recent Activity</SectionTitle>
          <MiniTable
            rows={detail.rows}
            cols={[
              { key: "underlying", label: "Ticker" },
              { key: "gate_reason", label: "Gate" },
              {
                key: "conviction",
                render: (v) => (v != null ? Number(v).toFixed(2) : "—"),
                label: "Conv",
              },
              {
                key: "created_at",
                render: (v) => String(v ?? "—").slice(0, 10),
                label: "Date",
              },
            ]}
          />
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// option_position.* node detail
// ---------------------------------------------------------------------------
function OptionPositionDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const pl = s.unrealized_pl as number | null | undefined;
  const plFormatted =
    pl != null
      ? `${pl >= 0 ? "+" : ""}$${Math.abs(pl).toFixed(2)}`
      : "—";
  const plColor = pl == null ? T.muted : pl >= 0 ? T.green : T.red;

  return (
    <>
      <SectionTitle>Option Position</SectionTitle>
      <KV k="underlying" v={String(s.underlying ?? s.ticker ?? "—")} />
      <KV k="contract" v={String(s.occ_symbol ?? "—")} />
      <KV k="side" v={String(s.side ?? "—")} />
      <KV k="contracts" v={String(s.contracts_qty ?? s.qty ?? "—")} />
      <KV k="entry premium" v={s.entry_premium != null ? `$${Number(s.entry_premium).toFixed(2)}` : "—"} />
      <KV k="delta" v={s.delta_at_open != null ? Number(s.delta_at_open).toFixed(3) : "—"} />
      <KV k="DTE" v={s.dte != null ? String(s.dte) : "—"} />
      <KV k="unrealized P&L" v={plFormatted} vColor={plColor} />
      {s.idea_id && (
        <>
          <SectionTitle>Origin</SectionTitle>
          <KV k="idea" v={String(s.idea_id)} />
        </>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// option_outcome.* node detail
// ---------------------------------------------------------------------------
function OptionOutcomeDetail({ detail }: { detail: NodeDetail }) {
  const s = detail.summary as Record<string, unknown>;
  const optPl = s.option_pl_pct as number | null | undefined;
  const alphaBps = s.underlying_alpha_bps as number | null | undefined;
  const optPlColor = optPl == null ? T.muted : optPl >= 0 ? T.green : T.red;
  const alphaColor = alphaBps == null ? T.muted : alphaBps >= 0 ? T.green : T.red;

  return (
    <>
      <SectionTitle>Option Outcome</SectionTitle>
      <KV k="underlying" v={String(s.underlying ?? "—")} />
      <KV k="contract" v={String(s.occ_symbol ?? "—")} />
      <KV k="side" v={String(s.side ?? "—")} />
      <KV k="close reason" v={String(s.close_reason ?? "—")} />
      <SectionTitle>P&L</SectionTitle>
      <KV
        k="option P&L %"
        v={optPl != null ? `${optPl >= 0 ? "+" : ""}${(optPl * 100).toFixed(1)}%` : "—"}
        vColor={optPlColor}
      />
      <KV
        k="underlying alpha (bps)"
        v={alphaBps != null ? `${alphaBps >= 0 ? "+" : ""}${alphaBps.toFixed(1)}` : "—"}
        vColor={alphaColor}
      />
      <KV k="entry premium" v={s.entry_premium != null ? `$${Number(s.entry_premium).toFixed(2)}` : "—"} />
      <KV k="exit premium" v={s.exit_premium != null ? `$${Number(s.exit_premium).toFixed(2)}` : "—"} />
      {s.idea_id && (
        <>
          <SectionTitle>Origin</SectionTitle>
          <KV k="idea" v={String(s.idea_id)} />
        </>
      )}
    </>
  );
}

function TypedDetail({ detail }: { detail: NodeDetail }) {
  // Check node id prefix before type, so opt.layer / option_* get dedicated views
  if (detail.id === "opt.layer") return <OptLayerDetail detail={detail} />;
  if (detail.id.startsWith("option_position.")) return <OptionPositionDetail detail={detail} />;
  if (detail.id.startsWith("option_outcome.")) return <OptionOutcomeDetail detail={detail} />;

  switch (detail.type as NodeType) {
    case "figure":
      return <FigureDetail detail={detail} />;
    case "advisor":
      return <AdvisorDetail detail={detail} />;
    case "idea":
      return <IdeaDetail detail={detail} />;
    case "trade":
      return <TradeDetail detail={detail} />;
    case "outcome":
      return <OutcomeDetail detail={detail} />;
    default:
      return <GenericDetail detail={detail} />;
  }
}

// ---------------------------------------------------------------------------
// Inspection Panel
// ---------------------------------------------------------------------------
const TYPE_COLORS: Record<NodeType, string> = {
  data_source: CLUSTER_COLOR.sources,
  figure: CLUSTER_COLOR.figures,
  advisor: CLUSTER_COLOR.council,
  engine_part: CLUSTER_COLOR.core,
  idea: CLUSTER_COLOR.ideas,
  exec_part: CLUSTER_COLOR.execution,
  trade: CLUSTER_COLOR.market,
  outcome: CLUSTER_COLOR.learning,
  infra: CLUSTER_COLOR.infra,
};

function InspectionPanel({
  id,
  onClose,
}: {
  id: string;
  onClose: () => void;
}) {
  const [detail, setDetail] = useState<NodeDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setDetail(null);
    setLoading(true);
    setError(null);
    fetchNode(id)
      .then((d) => {
        if (!alive) return;
        setDetail(d);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setError(e instanceof Error ? e.message : "fetch error");
        setLoading(false);
      });
    return () => {
      alive = false;
    };
  }, [id]);

  const typeColor = detail ? (TYPE_COLORS[detail.type] ?? T.muted) : T.muted;

  return (
    <div
      data-testid="inspection-panel"
      style={{
        // Floating rounded card with a 16px margin (matches the left-side HUD /
        // legend boxes) instead of a full-height slab flush to the screen edge —
        // so its content never gets clipped by the window edge.
        position: "absolute",
        top: 16,
        right: 48,
        width: 320,
        maxHeight: "calc(100vh - 32px)",
        background: T.panelBg,
        border: T.panelBorder,
        borderRadius: 10,
        overflowY: "auto" as const,
        overflowX: "hidden" as const,
        fontFamily: T.fontSans,
        fontSize: 13,
        color: T.text,
        boxSizing: "border-box",
        backdropFilter: "blur(8px)",
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "16px 18px 12px",
          borderBottom: T.panelBorder,
          position: "sticky",
          top: 0,
          background: T.panelBg,
          zIndex: 1,
        }}
      >
        <button
          onClick={onClose}
          aria-label="Close panel"
          style={{
            float: "right",
            background: "none",
            color: T.muted,
            border: 0,
            cursor: "pointer",
            fontSize: 16,
            lineHeight: 1,
            padding: "2px 4px",
          }}
        >
          ✕
        </button>
        {detail && (
          <div style={{ marginBottom: 4 }}>
            <Badge label={detail.type} color={typeColor} />
          </div>
        )}
        <div
          style={{
            fontWeight: 700,
            fontSize: 16,
            marginTop: 6,
            lineHeight: 1.3,
          }}
        >
          {detail?.label ?? id}
        </div>
      </div>

      {/* Body */}
      <div style={{ padding: "8px 18px 24px" }}>
        {loading && (
          <div style={{ color: T.muted, marginTop: 16, fontStyle: "italic" }}>
            Loading…
          </div>
        )}
        {error && (
          <div style={{ color: T.red, marginTop: 16 }}>
            Error: {error}
          </div>
        )}
        {detail && !loading && <TypedDetail detail={detail} />}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// HUD
// ---------------------------------------------------------------------------
function HUD({ state }: { state: State | null }) {
  const halted = state?.kill_switch.halted;
  const h = state?.health;
  const pl = fmtPL(state?.account.daily_pl);

  return (
    <div
      data-testid="hud"
      style={{
        position: "absolute",
        top: 16,
        left: 16,
        background: T.panelBg,
        border: T.panelBorder,
        borderRadius: T.radius,
        padding: "12px 16px",
        fontFamily: T.fontSans,
        fontSize: 12,
        lineHeight: 1.8,
        minWidth: 200,
        backdropFilter: "blur(8px)",
      }}
    >
      {/* Title */}
      <div
        style={{
          fontWeight: 700,
          letterSpacing: 1.4,
          fontSize: 10,
          color: T.muted,
          textTransform: "uppercase" as const,
          marginBottom: 8,
        }}
      >
        Arbiter Cockpit
      </div>
      <div
        style={{
          fontSize: 11,
          color: T.muted,
          marginTop: -4,
          marginBottom: 10,
          lineHeight: 1.5,
        }}
      >
        following smart money → council → trades
      </div>

      {/* Kill-switch banner */}
      {halted && (
        <div
          data-testid="halted-banner"
          style={{
            background: T.red,
            color: "#fff",
            fontWeight: 700,
            letterSpacing: 1,
            fontSize: 11,
            borderRadius: 4,
            padding: "3px 8px",
            marginBottom: 8,
            textAlign: "center" as const,
          }}
        >
          HALTED
        </div>
      )}

      {/* Account */}
      <div style={{ color: T.text }}>
        <div>
          <span style={{ color: T.muted }}>equity</span>{" "}
          <span style={{ fontWeight: 600 }}>
            {fmt(state?.account.equity, "$")}
          </span>
        </div>
        <div>
          <span style={{ color: T.muted }}>daily P&L</span>{" "}
          <span style={{ fontWeight: 600, color: pl.color }}>{pl.text}</span>
        </div>
      </div>

      {/* Health dots */}
      <div
        style={{
          marginTop: 8,
          paddingTop: 8,
          borderTop: T.panelBorder,
          display: "flex",
          gap: 10,
          fontSize: 11,
        }}
      >
        <span>
          <Dot ok={h?.db} /> db
        </span>
        <span>
          <Dot ok={h?.daemon} /> daemon
        </span>
        <span>
          <Dot ok={h?.alpaca} /> alpaca
        </span>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Legend
// ---------------------------------------------------------------------------
const CLUSTER_LABELS: Record<Cluster, string> = {
  sources: "Data Sources",
  figures: "Tracked Figures",
  council: "Advisor Council",
  core: "Decision Core",
  ideas: "Ideas / Theses",
  execution: "Execution",
  market: "Live Trades",
  learning: "Outcomes / Learning",
  infra: "Infrastructure",
  options: "Options Expression",
};

const NODE_TYPE_DESCRIPTIONS: Partial<Record<NodeType, string>> = {
  data_source: "External data feed",
  figure: "Politician / insider / fund",
  advisor: "A1 / A2 scoring advisor",
  engine_part: "Fusion, sizing, gate logic",
  idea: "Thesis with FSM lifecycle",
  exec_part: "Adapter, reconciler, exits",
  trade: "Live Alpaca position",
  outcome: "Realized result → trust update",
  infra: "Heartbeat, kill-switch, breakers",
};

function Legend({ visible, onToggle }: { visible: boolean; onToggle: () => void }) {
  return (
    <div
      style={{
        position: "absolute",
        // Lifted above a typical macOS Dock so the legend's bottom (Node Types)
        // stays visible even when the window underlaps the Dock.
        bottom: 84,
        left: 16,
        fontFamily: T.fontSans,
        fontSize: 12,
      }}
    >
      <button
        onClick={onToggle}
        style={{
          background: T.panelBg,
          border: T.panelBorder,
          borderRadius: T.radius,
          color: T.muted,
          cursor: "pointer",
          fontSize: 11,
          letterSpacing: 0.8,
          padding: "6px 12px",
        }}
      >
        {visible ? "Hide" : "Legend"}
      </button>

      {visible && (
        <div
          data-testid="legend"
          style={{
            position: "absolute",
            bottom: 36,
            left: 0,
            background: T.panelBg,
            border: T.panelBorder,
            borderRadius: T.radius,
            padding: "12px 14px",
            width: 222,
            lineHeight: 1.45,
            maxHeight: "calc(100vh - 90px)",
            overflowY: "auto",
            backdropFilter: "blur(8px)",
          }}
        >
          <div
            style={{
              fontSize: 10,
              letterSpacing: 1.2,
              color: T.muted,
              textTransform: "uppercase" as const,
              marginBottom: 6,
            }}
          >
            Cluster Colors
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", columnGap: 10, rowGap: 3 }}>
            {(Object.entries(CLUSTER_COLOR) as [Cluster, string][]).map(
              ([cluster, color]) => (
                <div
                  key={cluster}
                  style={{ display: "flex", alignItems: "center", gap: 6 }}
                >
                  <span
                    style={{
                      display: "inline-block",
                      width: 9,
                      height: 9,
                      borderRadius: "50%",
                      background: color,
                      flexShrink: 0,
                    }}
                  />
                  <span style={{ color: T.text, fontSize: 10.5 }}>
                    {CLUSTER_LABELS[cluster]}
                  </span>
                </div>
              )
            )}
          </div>

          <div
            style={{
              fontSize: 10,
              letterSpacing: 1.2,
              color: T.muted,
              textTransform: "uppercase" as const,
              marginTop: 10,
              marginBottom: 6,
            }}
          >
            Node Types
          </div>
          {/* Compact 2-column grid of color-coded type names (hover a node in the
              scene for its full description). */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "1fr 1fr",
              columnGap: 10,
              rowGap: 3,
            }}
            title="Hover any node in the scene for its description"
          >
            {(Object.keys(NODE_TYPE_DESCRIPTIONS) as NodeType[]).map((t) => (
              <div key={t} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 10.5 }}>
                <span
                  style={{
                    display: "inline-block",
                    width: 7,
                    height: 7,
                    borderRadius: 2,
                    background: TYPE_COLORS[t] ?? T.muted,
                    flexShrink: 0,
                  }}
                />
                <span style={{ color: T.text }}>{t}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Hover Tooltip
// ---------------------------------------------------------------------------
/**
 * SEAM: Lane 3 sets hoveredId in the store:
 *   import { useCockpitStore } from "../ui/store";
 *   useCockpitStore.getState().setHoveredId(node.id);   // on pointer-over
 *   useCockpitStore.getState().setHoveredId(null);      // on pointer-out
 *
 * The tooltip positions itself at a fixed corner because R3F canvas pointers are
 * canvas-relative; Lane 3 can optionally also set a screen-space position by extending
 * the store if a more precise position is needed.
 */
function HoverTooltip({ nodeState }: { nodeState: State | null }) {
  const hoveredId = useCockpitStore((s) => s.hoveredId);

  if (!hoveredId) return null;

  const ns = nodeState?.nodes[hoveredId];
  const intensity = ns?.intensity;

  return (
    <div
      data-testid="hover-tooltip"
      style={{
        position: "absolute",
        // Bottom-center (the positions panel moved to the top) — lifted above
        // a typical Dock so it stays visible.
        bottom: 84,
        left: "50%",
        transform: "translateX(-50%)",
        background: T.panelBg,
        border: T.panelBorder,
        borderRadius: 6,
        padding: "6px 12px",
        fontSize: 12,
        fontFamily: T.fontSans,
        color: T.text,
        pointerEvents: "none",
        zIndex: 10,
        whiteSpace: "nowrap" as const,
      }}
    >
      <span style={{ fontWeight: 600 }}>{hoveredId}</span>
      {ns?.status && (
        <span style={{ color: T.muted, marginLeft: 8 }}>{ns.status}</span>
      )}
      {intensity != null && (
        <span style={{ color: T.accent, marginLeft: 8 }}>
          {(intensity * 100).toFixed(0)}%
        </span>
      )}
      {ns?.label_extra && (
        <span style={{ color: T.muted, marginLeft: 8 }}>{ns.label_extra}</span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Guided "Follow the Money" Walkthrough
// ---------------------------------------------------------------------------

/** One narrated step. ``nodeId`` is the REAL graph node to select (or null →
 *  narration only, no inspection panel). */
type WalkStep = {
  nodeId: string | null;
  label: string;
  clusterHint: Cluster;
  narration: string;
};

/** Build the "follow the money" path from the LIVE graph + state so every step
 *  points at a node that actually exists (no more /node/figure.pelosi → 404).
 *  Picks the most-active tracked figure → its advisor → the decision core → a
 *  live idea → execution → a live trade → a live outcome. Steps with no live
 *  node (e.g. no open idea yet) are narration-only. */
function buildWalkthrough(graph: Graph | undefined, state: State | null): WalkStep[] {
  const dyn = state?.dynamic_nodes ?? [];
  const figures = (graph?.nodes ?? []).filter((n) => n.type === "figure");
  const figure = figures
    .slice()
    .sort(
      (a, b) =>
        Number((b.meta as Record<string, unknown>)?.n_filings ?? 0) -
        Number((a.meta as Record<string, unknown>)?.n_filings ?? 0),
    )[0];
  const figSource = String((figure?.meta as Record<string, unknown>)?.source ?? "congress");
  const advisorId =
    figSource === "form4" ? "A1.insider" : figSource === "form13d" ? "A1.activist" : "A1.congress";
  const advisorLabel =
    figSource === "form4" ? "A1 · Insiders" : figSource === "form13d" ? "A1 · Activists" : "A1 · Congress";

  const idea = dyn.find((n) => n.type === "idea");
  const trade = dyn.find((n) => n.type === "trade");
  const outcome = dyn.find((n) => n.type === "outcome");

  return [
    {
      nodeId: figure?.id ?? null,
      label: figure?.label ?? "a tracked figure",
      clusterHint: "figures",
      narration:
        "A tracked figure — a politician, insider, or fund — discloses a trade via an SEC/Congress filing. That disclosure is our starting signal.",
    },
    {
      nodeId: advisorId,
      label: advisorLabel,
      clusterHint: "council",
      narration:
        "An A1 advisor scores the disclosure (stance + confidence from history), and A2·MiroFish adds its own independent read.",
    },
    {
      nodeId: "core.fusion",
      label: "Fusion",
      clusterHint: "core",
      narration:
        "Fusion blends the advisors' opinions, weighted by each one's earned trust. The council reaches a verdict.",
    },
    {
      nodeId: "core.sizing",
      label: "Sizing",
      clusterHint: "core",
      narration:
        "Sizing computes the position: quarter-Kelly × trust weights, capped by average daily volume and the risk book.",
    },
    {
      nodeId: idea?.id ?? null,
      label: idea ? `Idea · ${idea.label}` : "an idea",
      clusterHint: "ideas",
      narration:
        "An Idea crystallizes — ticker, thesis, horizon — and moves through its lifecycle toward execution.",
    },
    {
      nodeId: "exec.adapter",
      label: "Alpaca adapter",
      clusterHint: "execution",
      narration:
        "The execution adapter submits the order to Alpaca — once the kill-switch and circuit-breaker gates pass.",
    },
    {
      nodeId: "opt.layer",
      label: "A4 · Options",
      clusterHint: "options",
      narration:
        "When conviction is high and IV-rank allows, the options expression layer papers or executes a matched option position — a leveraged expression of the same thesis, tracked separately from equity.",
    },
    {
      nodeId: trade?.id ?? null,
      label: trade ? trade.label : "a live trade",
      clusterHint: "market",
      narration: trade
        ? `A live trade is open (${trade.label}). Unrealized P&L streams in from Alpaca every few seconds.`
        : "A live trade opens. Unrealized P&L streams in from Alpaca every few seconds.",
    },
    {
      nodeId: outcome?.id ?? null,
      label: outcome ? "Outcome" : "the learning loop",
      clusterHint: "learning",
      narration:
        "When the trade closes, an Outcome is recorded — alpha (bps), win/loss — and feeds back to re-size each advisor's and figure's trust.",
    },
  ];
}

function Walkthrough({ path }: { path: WalkStep[] }) {
  const step = useCockpitStore((s) => s.walkthroughStep);
  const setStep = useCockpitStore((s) => s.setWalkthroughStep);
  const setSelectedId = useCockpitStore((s) => s.setSelectedId);
  const setFocusCluster = useCockpitStore((s) => s.setFocusCluster);

  const [open, setOpen] = useState(false);

  const apply = (wt: WalkStep) => {
    // null nodeId → no inspection panel (avoids 404 for steps with no live node).
    setSelectedId(wt.nodeId);
    setFocusCluster(wt.clusterHint);
  };

  const start = () => {
    setOpen(true);
    setStep(0);
    apply(path[0]);
  };

  const close = () => {
    setOpen(false);
    setStep(null);
    setFocusCluster(null);
    setSelectedId(null);
  };

  const goTo = (idx: number) => {
    if (idx < 0 || idx >= path.length) return;
    setStep(idx);
    apply(path[idx]);
  };

  const current = step != null ? path[step] : null;

  return (
    <div
      style={{
        fontFamily: T.fontSans,
        width: "fit-content",
        alignSelf: "flex-end",
      }}
    >
      {!open && (
        <button
          data-testid="walkthrough-btn"
          onClick={start}
          style={{
            background: T.accent,
            color: "#fff",
            border: "none",
            borderRadius: T.radius,
            padding: "8px 16px",
            cursor: "pointer",
            fontSize: 12,
            fontWeight: 600,
            letterSpacing: 0.5,
          }}
        >
          Follow the Money ▶
        </button>
      )}

      {open && current && (
        <div
          data-testid="walkthrough-panel"
          style={{
            background: T.panelBg,
            border: T.panelBorder,
            borderRadius: T.radius,
            padding: "16px 18px",
            width: 320,
            fontSize: 13,
          }}
        >
          {/* Step indicator */}
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: 10,
            }}
          >
            <span
              style={{
                fontSize: 10,
                letterSpacing: 1.2,
                color: T.muted,
                textTransform: "uppercase" as const,
              }}
            >
              Follow the Money
            </span>
            <span style={{ color: T.muted, fontSize: 11 }}>
              {(step ?? 0) + 1} / {path.length}
            </span>
          </div>

          {/* Step dots */}
          <div style={{ display: "flex", gap: 4, marginBottom: 12 }}>
            {path.map((_, i) => (
              <div
                key={i}
                onClick={() => goTo(i)}
                style={{
                  flex: 1,
                  height: 3,
                  borderRadius: 2,
                  background: i <= (step ?? 0) ? T.accent : "#1c2233",
                  cursor: "pointer",
                }}
              />
            ))}
          </div>

          {/* Cluster badge + node */}
          <div style={{ marginBottom: 8 }}>
            <Badge
              label={current.clusterHint}
              color={CLUSTER_COLOR[current.clusterHint]}
            />
            <span style={{ color: T.muted, fontSize: 11, marginLeft: 8 }}>
              {current.label}
            </span>
          </div>

          {/* Narration */}
          <div
            style={{
              color: T.text,
              lineHeight: 1.6,
              fontSize: 13,
              marginBottom: 14,
            }}
          >
            {current.narration}
          </div>

          {/* Controls */}
          <div style={{ display: "flex", gap: 8 }}>
            <button
              onClick={() => goTo((step ?? 0) - 1)}
              disabled={(step ?? 0) === 0}
              style={{
                flex: 1,
                background: "none",
                border: T.panelBorder,
                borderRadius: 6,
                color: (step ?? 0) === 0 ? T.muted : T.text,
                cursor: (step ?? 0) === 0 ? "not-allowed" : "pointer",
                padding: "6px 0",
                fontSize: 12,
              }}
            >
              ← Prev
            </button>
            {(step ?? 0) < path.length - 1 ? (
              <button
                onClick={() => goTo((step ?? 0) + 1)}
                style={{
                  flex: 2,
                  background: T.accent,
                  border: "none",
                  borderRadius: 6,
                  color: "#fff",
                  cursor: "pointer",
                  padding: "6px 0",
                  fontSize: 12,
                  fontWeight: 600,
                }}
              >
                Next →
              </button>
            ) : (
              <button
                onClick={close}
                style={{
                  flex: 2,
                  background: T.green,
                  border: "none",
                  borderRadius: 6,
                  color: "#fff",
                  cursor: "pointer",
                  padding: "6px 0",
                  fontSize: 12,
                  fontWeight: 600,
                }}
              >
                Done ✓
              </button>
            )}
            <button
              onClick={close}
              style={{
                background: "none",
                border: T.panelBorder,
                borderRadius: 6,
                color: T.muted,
                cursor: "pointer",
                padding: "6px 10px",
                fontSize: 12,
              }}
            >
              ✕
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Root export — signature FROZEN
// ---------------------------------------------------------------------------
export function CockpitUI({
  state,
  selectedId,
  onClose,
  graph,
}: {
  state: State | null;
  selectedId: string | null;
  onClose: () => void;
  graph?: Graph;
}) {
  const [legendVisible, setLegendVisible] = useState(true);
  // Build the "follow the money" path from the live graph + state (real node ids).
  const walkPath = useMemo(() => buildWalkthrough(graph, state), [graph, state]);

  // Sync the prop-driven selectedId into the store (App.tsx → store)
  const storeSetSelectedId = useCockpitStore((s) => s.setSelectedId);
  const prevSelectedRef = useRef<string | null>(null);
  useEffect(() => {
    if (selectedId !== prevSelectedRef.current) {
      prevSelectedRef.current = selectedId;
      storeSetSelectedId(selectedId);
    }
  }, [selectedId, storeSetSelectedId]);

  // Also sync store → onClose when store clears the selection
  const storeSelectedId = useCockpitStore((s) => s.selectedId);
  const effectiveSelectedId = selectedId ?? storeSelectedId;

  return (
    <>
      <HUD state={state} />
      <HoverTooltip nodeState={state} />
      <PositionsPanel />
      <Legend
        visible={legendVisible}
        onToggle={() => setLegendVisible((v) => !v)}
      />
      {/* Right-column container: OptionsPanel (bottom) + Walkthrough (above it).
          flex-direction: column-reverse means DOM order = bottom→top in the visual stack.
          Children listed: OptionsPanel first (renders at bottom), Walkthrough second (above). */}
      <div
        style={{
          position: "absolute",
          bottom: 84,
          right: 48,
          display: "flex",
          flexDirection: "column-reverse",
          gap: 8,
          alignItems: "flex-end",
        }}
      >
        <OptionsPanel inspectionOpen={!!effectiveSelectedId} />
        <Walkthrough path={walkPath} />
      </div>
      {effectiveSelectedId && (
        <InspectionPanel
          id={effectiveSelectedId}
          onClose={() => {
            onClose();
            storeSetSelectedId(null);
          }}
        />
      )}
    </>
  );
}
