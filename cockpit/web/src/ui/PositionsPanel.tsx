/**
 * PositionsPanel — live open positions + portfolio stats (read-only).
 *
 * Bottom-center card: a summary strip (open count, gross/net exposure, total
 * unrealized P&L, equity) over a per-position table (cost/share, current price,
 * ROI %, unrealized P&L). Polls /positions every 5s. Collapsible.
 */
import { useEffect, useState } from "react";
import { fetchPositions } from "../api";
import type { OpenPosition, PositionsResponse } from "../contract";
import { theme } from "../theme/theme";

const C = {
  bg: "rgba(8,10,18,0.93)",
  border: "1px solid #1c2233",
  green: theme.ok,
  red: theme.bad,
  muted: theme.muted,
  text: theme.text,
  mono: "'JetBrains Mono','Fira Code',monospace",
};

function usd(v: number | null | undefined, signed = false): string {
  if (v == null) return "—";
  const s = `$${Math.abs(v).toFixed(2)}`;
  if (signed) return `${v >= 0 ? "+" : "−"}${s}`;
  return v < 0 ? `−${s}` : s;
}
function pct(v: number | null | undefined): string {
  if (v == null) return "—";
  return `${v >= 0 ? "+" : ""}${(v * 100).toFixed(2)}%`;
}
const plColor = (v: number | null | undefined) =>
  v == null ? C.muted : v >= 0 ? C.green : C.red;

function Stat({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", minWidth: 70 }}>
      <span style={{ fontSize: 9, letterSpacing: 1, color: C.muted, textTransform: "uppercase" }}>
        {label}
      </span>
      <span style={{ fontSize: 14, fontWeight: 700, color: color ?? C.text, fontFamily: C.mono }}>
        {value}
      </span>
    </div>
  );
}

function Row({ p }: { p: OpenPosition }) {
  return (
    <tr style={{ borderTop: "1px solid #161c2b" }}>
      <td style={{ padding: "4px 10px 4px 0", fontWeight: 700 }}>{p.ticker}</td>
      <td style={{ padding: "4px 10px 4px 0" }}>
        <span
          style={{
            fontSize: 10,
            fontWeight: 700,
            padding: "1px 6px",
            borderRadius: 4,
            color: p.side === "short" ? "#ffb4c0" : "#9ff0d0",
            background: p.side === "short" ? "rgba(239,71,111,0.16)" : "rgba(6,214,160,0.16)",
            textTransform: "uppercase",
          }}
        >
          {p.side}
        </span>
      </td>
      <td style={{ padding: "4px 10px 4px 0", textAlign: "right" }}>{p.qty}</td>
      <td style={{ padding: "4px 10px 4px 0", textAlign: "right" }}>{usd(p.avg_entry)}</td>
      <td style={{ padding: "4px 10px 4px 0", textAlign: "right" }}>{usd(p.current_price)}</td>
      <td style={{ padding: "4px 10px 4px 0", textAlign: "right", color: plColor(p.unrealized_pl_pct), fontWeight: 700 }}>
        {pct(p.unrealized_pl_pct)}
      </td>
      <td style={{ padding: "4px 0", textAlign: "right", color: plColor(p.unrealized_pl), fontWeight: 700 }}>
        {usd(p.unrealized_pl, true)}
      </td>
    </tr>
  );
}

export function PositionsPanel() {
  const [data, setData] = useState<PositionsResponse | null>(null);
  const [open, setOpen] = useState(true);

  useEffect(() => {
    let alive = true;
    const tick = () => fetchPositions().then((d) => alive && setData(d)).catch(() => {});
    tick();
    const id = setInterval(tick, 5000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  const pf = data?.portfolio;

  return (
    <div
      style={{
        position: "absolute",
        top: 16,
        left: "50%",
        transform: "translateX(-50%)",
        width: 560,
        background: C.bg,
        border: C.border,
        borderRadius: 10,
        padding: "10px 14px",
        fontFamily: "'Inter',system-ui,sans-serif",
        fontSize: 12,
        color: C.text,
        backdropFilter: "blur(8px)",
        maxHeight: "60vh",
        overflowY: "auto",
        zIndex: 5,
      }}
    >
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 8 }}>
        <span style={{ fontSize: 10, fontWeight: 800, letterSpacing: 1.6, color: C.muted, textTransform: "uppercase" }}>
          Open Positions{pf ? ` · ${pf.n_open}` : ""}
        </span>
        <button
          onClick={() => setOpen((v) => !v)}
          style={{ background: "none", border: 0, color: C.muted, cursor: "pointer", fontSize: 11 }}
        >
          {open ? "▾ hide" : "▸ show"}
        </button>
      </div>

      {/* Summary stats */}
      {pf && (
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap", marginBottom: open ? 10 : 0 }}>
          <Stat label="Open" value={`${pf.n_open}  ${pf.n_long}L / ${pf.n_short}S`} />
          <Stat label="Gross" value={usd(pf.gross_exposure)} />
          <Stat label="Net" value={usd(pf.net_exposure)} />
          <Stat label="Unreal P&L" value={usd(pf.total_unrealized_pl, true)} color={plColor(pf.total_unrealized_pl)} />
          <Stat label="ROI" value={pct(pf.total_unrealized_pl_pct)} color={plColor(pf.total_unrealized_pl_pct)} />
          <Stat label="Equity" value={usd(pf.equity)} />
        </div>
      )}

      {/* Per-position table */}
      {open && (
        data && !data.alpaca_ok ? (
          <div style={{ color: C.muted, fontStyle: "italic", padding: "6px 0" }}>
            broker offline — positions unavailable
          </div>
        ) : data && data.positions.length === 0 ? (
          <div style={{ color: C.muted, fontStyle: "italic", padding: "6px 0" }}>
            no open positions
          </div>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontFamily: C.mono, fontSize: 11.5 }}>
            <thead>
              <tr style={{ color: C.muted, textAlign: "left" }}>
                <th style={{ padding: "0 10px 4px 0", fontWeight: 600 }}>Ticker</th>
                <th style={{ padding: "0 10px 4px 0", fontWeight: 600 }}>Side</th>
                <th style={{ padding: "0 10px 4px 0", fontWeight: 600, textAlign: "right" }}>Shares</th>
                <th style={{ padding: "0 10px 4px 0", fontWeight: 600, textAlign: "right" }}>Cost/sh</th>
                <th style={{ padding: "0 10px 4px 0", fontWeight: 600, textAlign: "right" }}>Current</th>
                <th style={{ padding: "0 10px 4px 0", fontWeight: 600, textAlign: "right" }}>ROI</th>
                <th style={{ padding: "0 0 4px 0", fontWeight: 600, textAlign: "right" }}>P&L</th>
              </tr>
            </thead>
            <tbody>
              {(data?.positions ?? []).map((p) => <Row key={p.ticker} p={p} />)}
            </tbody>
          </table>
        )
      )}
    </div>
  );
}
