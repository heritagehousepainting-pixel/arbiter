/**
 * OptionsPanel — live options expression layer panel (read-only).
 *
 * Placement: bottom-right, stacked below Walkthrough inside a flex column-reverse
 * container in CockpitUI.tsx.  Position/size exact per layout plan:
 *   width: min(640px, calc(100vw - 96px))  — widened so the 8-column open
 *     positions table (Contract/Side/Bought/Current/Qty/DTE/ROI/P&L) never
 *     crams; matches the sibling PositionsPanel's roomier column spacing.
 *   maxHeight: 52vh
 *   Collapsed to 36px header strip when InspectionPanel is open (occlusion guard).
 *
 * Polls /options every 5s.  IV history collapsed by default, toggle to expand.
 */
import { Fragment, useRef, useEffect, useState } from "react";
import { fetchOptions, fetchTickerDetail } from "../api";
import type {
  OpenOptionPosition,
  OptionShadowPlay,
  OptionsMode,
  OptionsState,
  TickerDetail,
} from "../contract";
import { theme } from "../theme/theme";

// ---------------------------------------------------------------------------
// Design tokens — mirror CockpitUI token set
// ---------------------------------------------------------------------------
const C = {
  bg: "rgba(8,10,18,0.93)",
  border: "1px solid #1c2233",
  borderAmber: "1px solid rgba(249,168,37,0.30)",
  radius: 10,
  green: theme.ok,
  red: theme.bad,
  muted: theme.muted,
  text: theme.text,
  mono: "'JetBrains Mono','Fira Code',monospace",
  sans: "'Inter','Helvetica Neue',system-ui,sans-serif",
  amber: "#f9a825",
  amberDim: "#8d6e15",
  shadow: "#8d99ae",
} as const;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function usd(v: number | null | undefined, signed = false): string {
  if (v == null) return "—";
  const s = `$${Math.abs(v).toFixed(2)}`;
  if (signed) return `${v >= 0 ? "+" : "−"}${s}`;
  return v < 0 ? `−${s}` : s;
}

function pct(v: number | null | undefined, digits = 1): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(digits)}%`;
}

function fmt(v: number | null | undefined, digits = 2): string {
  if (v == null) return "—";
  return v.toFixed(digits);
}

function plColor(v: number | null | undefined): string {
  if (v == null) return C.muted;
  return v >= 0 ? C.green : C.red;
}

// Format an ISO expiry "2027-06-17" → "06/17/27" (MM/DD/YY). The YEAR is kept
// on purpose: the options layer trades long-dated LEAPS (often ~1yr out), so a
// bare "06/17" reads as already-expired. Degrades safely on bad input.
export function fmtExpiry(iso: string | null | undefined): string {
  if (!iso) return "?";
  const [y, m, d] = iso.split("-");
  if (!y || !m || !d) return iso;
  return `${m}/${d}/${y.slice(2)}`;
}

// Format OCC-style contract label: underlying + strike + C/P + expiry MM/DD/YY
function contractLabel(p: OpenOptionPosition): string {
  const cp = p.side === "call" ? "C" : "P";
  return `${p.underlying} ${p.strike}${cp} ${fmtExpiry(p.expiry)}`;
}

function shadowContractLabel(p: OptionShadowPlay): string {
  if (!p.strike || !p.expiry || !p.side) return p.underlying;
  const cp = p.side === "call" ? "C" : "P";
  return `${p.underlying} ${p.strike}${cp} ${fmtExpiry(p.expiry)}`;
}

// ---------------------------------------------------------------------------
// Mode badge
// ---------------------------------------------------------------------------
function ModeBadge({ mode }: { mode: OptionsMode }) {
  const label = mode.toUpperCase();
  const bg =
    mode === "paper"
      ? C.amber
      : mode === "shadow"
        ? "rgba(141,153,174,0.25)"
        : "rgba(255,255,255,0.05)";
  const color =
    mode === "paper" ? "#000" : mode === "shadow" ? C.shadow : C.muted;

  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px 7px",
        borderRadius: 4,
        fontSize: 10,
        fontWeight: 700,
        background: bg,
        color,
        letterSpacing: 0.5,
        textTransform: "uppercase" as const,
      }}
    >
      {label}
    </span>
  );
}

// ---------------------------------------------------------------------------
// Section title — same style as CockpitUI.SectionTitle
// ---------------------------------------------------------------------------
function SectionTitle({ children }: { children: React.ReactNode }) {
  return (
    <div
      style={{
        fontSize: 10,
        letterSpacing: 1.4,
        color: C.muted,
        textTransform: "uppercase" as const,
        marginTop: 12,
        marginBottom: 5,
        borderBottom: C.border,
        paddingBottom: 3,
      }}
    >
      {children}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Stats strip
// ---------------------------------------------------------------------------
function StatsStrip({ data }: { data: OptionsState }) {
  const sleeveUsed =
    data.sleeve_used_pct != null ? `${(data.sleeve_used_pct * 100).toFixed(0)}%` : "—";

  // Determine IV-rank gate status from the shadow plays
  const hasEnoughIV = data.recent_shadow_plays.some(
    (p) => p.ivr_estimate != null
  );
  const gateStatus = data.options_mode === "off" ? "off" : hasEnoughIV ? "active" : "building";

  return (
    <div
      style={{
        display: "flex",
        gap: 8,
        flexWrap: "wrap" as const,
        fontSize: 11,
        color: C.muted,
        paddingBottom: 8,
        borderBottom: C.border,
      }}
    >
      <span>sleeve 35%</span>
      <span style={{ color: C.text }}>·</span>
      <span>
        used{" "}
        <span style={{ color: C.text, fontWeight: 600 }}>{sleeveUsed}</span>
      </span>
      <span style={{ color: C.text }}>·</span>
      <span>
        IV-rank gate:{" "}
        <span
          style={{
            color:
              gateStatus === "active"
                ? C.amber
                : gateStatus === "building"
                  ? C.muted
                  : C.muted,
            fontWeight: 600,
          }}
        >
          {gateStatus}
        </span>
      </span>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Open option positions mini-table
// ---------------------------------------------------------------------------
// Coloured signed percent — matches the trade-detail style ("+4.2%" green /
// "-1.4%" red); the sign conveys direction, so no arrow.
function PctChip({ frac }: { frac: number | null | undefined }) {
  if (frac == null) return <span style={{ color: C.muted }}>—</span>;
  return <span style={{ color: plColor(frac), fontWeight: 700 }}>{pct(frac)}</span>;
}

// Per-contract (per-share) premium: entry_premium is the TOTAL paid for all
// contracts in the position, so divide out contracts_qty and the ×100
// multiplier to get the single-contract quote (e.g. "$0.75", matching how
// the option was quoted when bought). current_mid is already per-share.
function perContractEntry(p: OpenOptionPosition): number {
  return p.entry_premium / (p.contracts_qty * 100);
}

// Expanded tracking detail for one option (underlying live + since-open context).
function OptionDetailRow({
  p, detail, loading,
}: {
  p: OpenOptionPosition;
  detail: TickerDetail | undefined;
  loading: boolean;
}) {
  const cur = detail?.current_price ?? null;
  const sinceOpen =
    cur != null && p.underlying_open_price
      ? (cur - p.underlying_open_price) / p.underlying_open_price
      : null;
  let moneyness: string | null = null;
  if (cur != null) {
    const intrinsic = p.side === "put" ? p.strike - cur : cur - p.strike;
    moneyness = `${intrinsic >= 0 ? "ITM" : "OTM"} by ${usd(Math.abs(intrinsic))}`;
  }
  const lbl = { color: C.muted, minWidth: 64, display: "inline-block" } as const;
  return (
    <tr>
      <td colSpan={8} style={{ padding: "2px 0 11px 18px" }}>
        {loading ? (
          <span style={{ color: C.muted, fontStyle: "italic", fontSize: 11 }}>loading…</span>
        ) : (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, fontSize: 11, lineHeight: 1.6 }}>
            <div style={{ color: C.text, fontWeight: 700, fontSize: 12 }}>
              {detail?.name ?? p.underlying}
            </div>
            <div><span style={lbl}>Today</span>{cur != null ? usd(cur) : "—"} &nbsp;<PctChip frac={detail?.day_change_pct} /></div>
            <div><span style={lbl}>1-Month</span><PctChip frac={detail?.month_return_pct} /></div>
            <div style={{ color: C.muted, fontSize: 9.5, letterSpacing: 1, marginTop: 4 }}>SINCE YOU OPENED</div>
            <div>
              <span style={lbl}>Underlying</span>
              {usd(p.underlying_open_price)} → {cur != null ? usd(cur) : "—"} &nbsp;<PctChip frac={sinceOpen} />
            </div>
            <div>
              <span style={lbl}>Strike</span>
              {usd(p.strike)}{moneyness ? ` · ${moneyness}` : ""} · Δ {fmt(p.delta_at_open, 2)}
            </div>
            <div>
              <span style={lbl}>Cost basis</span>
              {usd(p.entry_premium)} total · conviction {fmt(p.original_conviction, 2)}
            </div>
          </div>
        )}
      </td>
    </tr>
  );
}

export function OpenPositionsTable({
  positions,
}: {
  positions: OpenOptionPosition[];
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [detailMap, setDetailMap] = useState<Map<string, TickerDetail>>(new Map());
  const detailCache = useRef<Map<string, TickerDetail>>(new Map());

  if (positions.length === 0) {
    return (
      <div style={{ color: C.muted, fontStyle: "italic", fontSize: 12 }}>
        no open option positions
      </div>
    );
  }

  const handleToggle = (p: OpenOptionPosition) => {
    const collapse = openId === p.id;
    setOpenId(collapse ? null : p.id);
    if (!collapse && !detailCache.current.has(p.underlying)) {
      fetchTickerDetail(p.underlying)
        .then((d) => {
          detailCache.current.set(p.underlying, d);
          setDetailMap(new Map(detailCache.current));
        })
        .catch(() => {
          // cache an empty detail so the row degrades to "—" instead of spinning
          detailCache.current.set(p.underlying, {
            symbol: p.underlying, name: null, month_return_pct: null,
            day_change_pct: null, current_price: null, as_of: "",
          });
          setDetailMap(new Map(detailCache.current));
        });
    }
  };

  return (
    <div style={{ overflowX: "auto" as const }}>
      <table
        style={{
          width: "100%",
          borderCollapse: "collapse",
          fontSize: 11.5,
          fontFamily: C.mono,
        }}
      >
        <thead>
          <tr style={{ color: C.muted }}>
            <th style={{ textAlign: "left" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>Contract</th>
            <th style={{ textAlign: "left" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>Side</th>
            <th style={{ textAlign: "right" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>Bought</th>
            <th style={{ textAlign: "right" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>Current</th>
            <th style={{ textAlign: "right" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>Qty</th>
            <th style={{ textAlign: "right" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>DTE</th>
            <th style={{ textAlign: "right" as const, padding: "0 12px 5px 0", fontWeight: 600 }}>ROI</th>
            <th style={{ textAlign: "right" as const, padding: "0 0 5px 0", fontWeight: 600 }}>P&L</th>
          </tr>
        </thead>
        <tbody>
          {positions.map((p) => {
            const pl = p.unrealized_pl;
            const isOpen = openId === p.id;
            return (
              <Fragment key={p.id}>
              <tr style={{ borderTop: C.border }}>
                <td style={{ padding: "6px 12px 6px 0", whiteSpace: "nowrap" as const }}>
                  <button
                    onClick={() => handleToggle(p)}
                    aria-expanded={isOpen}
                    style={{
                      background: "none", border: 0, padding: 0, cursor: "pointer",
                      color: C.amber, fontWeight: 600, fontFamily: C.mono, fontSize: 11.5,
                    }}
                  >
                    {isOpen ? "▾" : "▸"} {contractLabel(p)}
                  </button>
                </td>
                <td style={{ padding: "6px 12px 6px 0" }}>
                  <span
                    style={{
                      fontSize: 9.5,
                      fontWeight: 700,
                      padding: "2px 6px",
                      borderRadius: 3,
                      color: p.side === "put" ? "#ffb4c0" : "#9ff0d0",
                      background:
                        p.side === "put"
                          ? "rgba(239,71,111,0.16)"
                          : "rgba(6,214,160,0.16)",
                      textTransform: "uppercase" as const,
                    }}
                  >
                    {p.side}
                  </span>
                </td>
                <td style={{ padding: "6px 12px 6px 0", textAlign: "right" as const, color: C.muted, whiteSpace: "nowrap" as const }}>
                  {usd(perContractEntry(p))}
                </td>
                <td style={{
                  padding: "6px 12px 6px 0", textAlign: "right" as const, whiteSpace: "nowrap" as const,
                  color: p.current_mid == null ? C.muted : plColor(p.unrealized_pl_pct),
                  fontWeight: 600,
                }}>
                  {p.current_mid != null ? usd(p.current_mid) : "—"}
                </td>
                <td style={{ padding: "6px 12px 6px 0", textAlign: "right" as const, color: C.text }}>
                  {p.contracts_qty}
                </td>
                <td style={{ padding: "6px 12px 6px 0", textAlign: "right" as const, color: C.muted }}>
                  {p.dte ?? "—"}
                </td>
                <td style={{
                  padding: "6px 12px 6px 0", textAlign: "right" as const,
                  color: p.unrealized_pl_pct == null ? C.muted : plColor(p.unrealized_pl_pct),
                  fontWeight: 700,
                }}>
                  {pct(p.unrealized_pl_pct)}
                </td>
                <td
                  style={{
                    padding: "6px 0",
                    textAlign: "right" as const,
                    color: plColor(pl),
                    fontWeight: 700,
                    whiteSpace: "nowrap" as const,
                  }}
                >
                  {usd(pl, true)}
                </td>
              </tr>
              {isOpen && (
                <OptionDetailRow
                  p={p}
                  detail={detailMap.get(p.underlying)}
                  loading={!detailMap.has(p.underlying)}
                />
              )}
              </Fragment>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recent plays (shadow + paper) mini-table
// ---------------------------------------------------------------------------
function RecentPlaysTable({ plays }: { plays: OptionShadowPlay[] }) {
  if (plays.length === 0) {
    return (
      <div style={{ color: C.muted, fontStyle: "italic", fontSize: 12 }}>
        building IV history…
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
          fontFamily: C.mono,
        }}
      >
        <thead>
          <tr style={{ color: C.muted }}>
            <th style={{ textAlign: "left" as const, padding: "0 10px 5px 0", fontWeight: 600 }}>Contract</th>
            <th style={{ textAlign: "left" as const, padding: "0 10px 5px 0", fontWeight: 600 }}>Type</th>
            <th style={{ textAlign: "left" as const, padding: "0 10px 5px 0", fontWeight: 600 }}>Gate</th>
            <th style={{ textAlign: "right" as const, padding: "0 10px 5px 0", fontWeight: 600 }}>Conv</th>
            <th style={{ textAlign: "left" as const, padding: "0 0 5px 0", fontWeight: 600 }}>Tag</th>
          </tr>
        </thead>
        <tbody>
          {plays.map((p) => (
            <tr key={p.id} style={{ borderTop: C.border }}>
              <td style={{ padding: "6px 10px 6px 0", color: C.text }}>
                {shadowContractLabel(p)}
              </td>
              <td style={{ padding: "6px 10px 6px 0" }}>
                <span
                  style={{
                    fontSize: 9,
                    fontWeight: 700,
                    padding: "1px 5px",
                    borderRadius: 3,
                    color: p.gate_express ? C.amber : C.shadow,
                    background: p.gate_express
                      ? "rgba(249,168,37,0.12)"
                      : "rgba(141,153,174,0.10)",
                  }}
                >
                  {p.gate_express ? "paper" : "shadow"}
                </span>
              </td>
              <td
                style={{
                  padding: "6px 10px 6px 0",
                  color: C.muted,
                  fontSize: 10,
                  maxWidth: 90,
                  overflow: "hidden" as const,
                  textOverflow: "ellipsis" as const,
                  whiteSpace: "nowrap" as const,
                }}
                title={p.gate_reason}
              >
                {p.gate_reason ?? "—"}
              </td>
              <td style={{ padding: "6px 10px 6px 0", textAlign: "right" as const, color: C.text }}>
                {fmt(p.conviction, 2)}
              </td>
              <td style={{ padding: "6px 0", color: C.muted, fontSize: 10 }}>
                {p.catalyst_tag ?? "—"}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// IV mini-summary section
// ---------------------------------------------------------------------------
function IVSummary({
  plays,
  open: sectionOpen,
  onToggle,
}: {
  plays: OptionShadowPlay[];
  open: boolean;
  onToggle: () => void;
}) {
  // Collect per-ticker latest IVR estimate
  const byTicker: Record<string, { ivr: number | null; iv: number | null }> = {};
  for (const p of plays) {
    if (!byTicker[p.underlying]) {
      byTicker[p.underlying] = { ivr: p.ivr_estimate, iv: p.iv };
    }
  }
  const tickers = Object.entries(byTicker);

  return (
    <>
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          marginTop: 12,
          marginBottom: sectionOpen ? 5 : 0,
        }}
      >
        <span
          style={{
            fontSize: 10,
            letterSpacing: 1.4,
            color: C.muted,
            textTransform: "uppercase" as const,
            borderBottom: sectionOpen ? C.border : "none",
            paddingBottom: sectionOpen ? 3 : 0,
            flex: 1,
          }}
        >
          IV History
        </span>
        <button
          onClick={onToggle}
          style={{
            background: "none",
            border: 0,
            color: C.muted,
            cursor: "pointer",
            fontSize: 11,
            marginLeft: 8,
          }}
        >
          {sectionOpen ? "▾ hide" : "▸ show"}
        </button>
      </div>

      {sectionOpen && (
        tickers.length === 0 ? (
          <div style={{ color: C.muted, fontStyle: "italic", fontSize: 12 }}>
            building IV history…
          </div>
        ) : (
          <div style={{ overflowX: "auto" as const }}>
            <table
              style={{
                width: "100%",
                borderCollapse: "collapse",
                fontSize: 11,
                fontFamily: C.mono,
              }}
            >
              <thead>
                <tr style={{ color: C.muted }}>
                  <th style={{ textAlign: "left" as const, padding: "0 10px 5px 0", fontWeight: 600 }}>Ticker</th>
                  <th style={{ textAlign: "right" as const, padding: "0 10px 5px 0", fontWeight: 600 }}>ATM IV</th>
                  <th style={{ textAlign: "right" as const, padding: "0 0 5px 0", fontWeight: 600 }}>IVR</th>
                </tr>
              </thead>
              <tbody>
                {tickers.map(([ticker, { ivr, iv }]) => (
                  <tr key={ticker} style={{ borderTop: C.border }}>
                    <td style={{ padding: "6px 10px 6px 0", color: C.amber }}>{ticker}</td>
                    <td style={{ padding: "6px 10px 6px 0", textAlign: "right" as const, color: C.text }}>
                      {iv != null ? `${(iv * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td
                      style={{
                        padding: "6px 0",
                        textAlign: "right" as const,
                        color: ivr != null && ivr >= 0.5 ? C.amber : C.muted,
                        fontWeight: ivr != null && ivr >= 0.5 ? 600 : 400,
                      }}
                    >
                      {ivr != null ? `${(ivr * 100).toFixed(0)}th` : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Win-rate / aggregate stats
// ---------------------------------------------------------------------------
function AggStats({ data }: { data: OptionsState }) {
  const { win_rate, avg_option_pl_pct, n_open } = data;
  if (win_rate == null && avg_option_pl_pct == null && n_open === 0) return null;

  return (
    <div
      style={{
        display: "flex",
        gap: 12,
        flexWrap: "wrap" as const,
        fontSize: 11,
        color: C.muted,
        marginTop: 8,
        paddingTop: 8,
        borderTop: C.border,
      }}
    >
      {win_rate != null && (
        <span>
          win{" "}
          <span style={{ color: win_rate >= 0.5 ? C.green : C.red, fontWeight: 600 }}>
            {pct(win_rate, 0)}
          </span>
        </span>
      )}
      {avg_option_pl_pct != null && (
        <span>
          avg P&L{" "}
          <span style={{ color: plColor(avg_option_pl_pct), fontWeight: 600 }}>
            {pct(avg_option_pl_pct)}
          </span>
        </span>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// OptionsPanel — main component
// ---------------------------------------------------------------------------
export interface OptionsPanelProps {
  /** When truthy, auto-collapse the panel body to avoid InspectionPanel overlap */
  inspectionOpen: boolean;
}

export function OptionsPanel({ inspectionOpen }: OptionsPanelProps) {
  const [data, setData] = useState<OptionsState | null>(null);
  const [open, setOpen] = useState(true);
  const [ivOpen, setIvOpen] = useState(false);

  // Poll /options every 5s alongside the existing /state poll
  useEffect(() => {
    let alive = true;
    const tick = () =>
      fetchOptions()
        .then((d) => alive && setData(d))
        .catch(() => {});
    tick();
    const id = setInterval(tick, 5000);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, []);

  // Occlusion guard: auto-collapse when InspectionPanel opens
  useEffect(() => {
    if (inspectionOpen) setOpen(false);
  }, [inspectionOpen]);

  // Default-collapse when options_mode = off
  useEffect(() => {
    if (data?.options_mode === "off") setOpen(false);
  }, [data?.options_mode]);

  // Occlusion guard #2 — horizontal overlap with the top-center Positions panel.
  // That panel is 560px wide, centered (`left:50%`), so its right edge is at
  // `50vw + 280`; this panel's left edge is at `100vw - 688` (width 640, right 48).
  // They overlap once `50vw + 280 > 100vw - 688`, i.e. below ~1936px wide.  When
  // that happens we cap the body so the panel's top stays clear of the Positions
  // panel's worst-case footprint (its 60vh scroll cap) instead of rising under it.
  const [vw, setVw] = useState<number>(
    typeof window !== "undefined" ? window.innerWidth : 1512,
  );
  useEffect(() => {
    const onResize = () => setVw(window.innerWidth);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);
  // top = vh - 84(container bottom) - 36(header) - body; require it ≥ 16 + 60vh + 12
  // gap → body ≤ 40vh - 148px.  Above the threshold the original 52vh is used.
  const bodyMaxHeight = vw < 1936 ? "calc(40vh - 148px)" : "52vh";

  const mode = data?.options_mode ?? "off";
  const nOpen = data?.n_open ?? 0;
  const isPaper = mode === "paper";

  // Amber glow border when paper mode is active
  const panelBorder = isPaper ? C.borderAmber : C.border;

  return (
    <div
      data-testid="options-panel"
      style={{
        width: "min(640px, calc(100vw - 96px))",
        background: C.bg,
        border: panelBorder,
        borderRadius: C.radius,
        fontFamily: C.sans,
        fontSize: 12,
        color: C.text,
        backdropFilter: "blur(8px)",
        overflow: "hidden" as const,
        // maxHeight handled by the body div, not the container, so the header
        // stays visible when collapsed.
      }}
    >
      {/* ── Header strip (always visible, 36px) ─────────────────────────── */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "0 12px",
          height: 36,
          borderBottom: open ? C.border : "none",
          flexShrink: 0,
        }}
      >
        <span
          style={{
            fontSize: 10,
            fontWeight: 800,
            letterSpacing: 1.6,
            color: isPaper ? C.amber : C.muted,
            textTransform: "uppercase" as const,
            flexShrink: 0,
          }}
        >
          Options
        </span>
        <ModeBadge mode={mode} />
        {nOpen > 0 && (
          <span
            style={{
              fontSize: 10,
              fontWeight: 700,
              color: C.amber,
              flexShrink: 0,
            }}
          >
            ● {nOpen}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button
          onClick={() => setOpen((v) => !v)}
          style={{
            background: "none",
            border: 0,
            color: C.muted,
            cursor: "pointer",
            fontSize: 11,
            padding: "0 0 0 4px",
            flexShrink: 0,
          }}
        >
          {open ? "▾ hide" : "▸ show"}
        </button>
      </div>

      {/* ── Scrollable body ─────────────────────────────────────────────── */}
      {open && (
        <div
          style={{
            maxHeight: bodyMaxHeight,
            overflowY: "auto" as const,
            padding: "8px 12px 14px",
          }}
        >
          {!data ? (
            <div style={{ color: C.muted, fontStyle: "italic", paddingTop: 4 }}>
              loading…
            </div>
          ) : (
            <>
              {/* Stats strip */}
              <StatsStrip data={data} />

              {/* Aggregate outcomes */}
              <AggStats data={data} />

              {/* Open option positions */}
              <SectionTitle>Open Option Positions</SectionTitle>
              <OpenPositionsTable positions={data.open_positions} />

              {/* Recent plays */}
              <SectionTitle>Recent Plays (last {data.recent_shadow_plays.length || 5})</SectionTitle>
              <RecentPlaysTable plays={data.recent_shadow_plays} />

              {/* IV History — collapsed by default */}
              <IVSummary
                plays={data.recent_shadow_plays}
                open={ivOpen}
                onToggle={() => setIvOpen((v) => !v)}
              />

              {/* Footer affordance pointing to option-outcome nodes */}
              <div
                style={{
                  marginTop: 14,
                  paddingTop: 10,
                  borderTop: C.border,
                  fontSize: 11,
                  color: C.muted,
                  fontStyle: "italic",
                }}
              >
                Click an option outcome node in the constellation for P&L history →
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
}
