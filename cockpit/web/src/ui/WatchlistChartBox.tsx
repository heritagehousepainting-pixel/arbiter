/**
 * WatchlistChartBox — floating chart panel for the active watched ticker.
 *
 * Placement: position:absolute, top:16, right:16, zIndex:18
 * Width:     min(500px, calc(100vw - 96px))
 *
 * Reads `activeWatchSymbol` from useWatchlistStore.
 * Returns null when no symbol is active — mount unconditionally in CockpitUI.
 *
 * Architecture:
 *   • Fetches TickerDetail (name, price, day%) once per symbol
 *   • Fetches ChartSeries lazily per (symbol, range) — cached in a useRef Map
 *   • Thumbnail strip always shows 5D/1M/3M/6M; clicking promotes to primary chart
 *   • Pre/post-market toggle (live range only); hidden on historical ranges
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { fetchChart, fetchTickerDetail } from "../api";
import type { ChartRange, ChartSeries, TickerDetail } from "../contract";
import { motion, prefersReducedMotion } from "../theme/theme";
import { useWatchlistStore } from "./watchlistStore";
import { CandleChart } from "./CandleChart";

// ---------------------------------------------------------------------------
// Design tokens (mirrors CockpitUI/OptionsPanel token set)
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

const THUMBNAIL_RANGES: ChartRange[] = ["5d", "1m", "3m", "6m"];
const RANGE_LABELS: Record<ChartRange, string> = {
  live: "Live",
  "5d": "5D",
  "1m": "1M",
  "3m": "3M",
  "6m": "6M",
};

// Cache key
function cacheKey(sym: string, range: ChartRange): string {
  return `${sym}:${range}`;
}

// ---------------------------------------------------------------------------
// Subcomponents
// ---------------------------------------------------------------------------

function RangeTab({
  range,
  active,
  onClick,
}: {
  range: ChartRange;
  active: boolean;
  onClick: () => void;
}) {
  const isLive = range === "live";
  return (
    <button
      role="tab"
      aria-selected={active}
      onClick={onClick}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        padding: "2px 9px",
        borderRadius: 4,
        border: active ? `1px solid ${T.accent}` : "1px solid rgba(28,34,51,0.8)",
        background: active ? "rgba(124,131,255,0.15)" : "rgba(255,255,255,0.04)",
        color: active ? T.accent : T.muted,
        fontSize: 11,
        fontWeight: 600,
        fontFamily: T.sans,
        cursor: "pointer",
        letterSpacing: 0.4,
        transition: `background ${motion.fast}ms ease-out, color ${motion.fast}ms ease-out`,
      }}
    >
      {isLive && (
        <span style={{ color: T.green, fontSize: 9 }}>●</span>
      )}
      {RANGE_LABELS[range]}
    </button>
  );
}

function ThumbnailChart({
  sym,
  range,
  series,
  active,
  onClick,
}: {
  sym: string;
  range: ChartRange;
  series: ChartSeries | null;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      aria-label={`${RANGE_LABELS[range]}-chart for ${sym}, click to expand`}
      onClick={onClick}
      style={{
        flex: 1,
        height: 52,
        border: active
          ? "1px solid rgba(124,131,255,0.5)"
          : T.border,
        borderRadius: 6,
        background: "rgba(28,34,51,0.7)",
        cursor: "pointer",
        padding: 0,
        overflow: "hidden",
        position: "relative",
      }}
    >
      {series ? (
        <CandleChart candles={series.candles} showExtended={false} height={44} />
      ) : (
        <span
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            height: "100%",
            fontSize: 9,
            color: T.muted,
            fontStyle: "italic",
          }}
        >
          …
        </span>
      )}
      <span
        style={{
          position: "absolute",
          bottom: 2,
          left: 4,
          fontSize: 9,
          color: T.muted,
          fontFamily: T.sans,
          textTransform: "uppercase",
          letterSpacing: 0.8,
          pointerEvents: "none",
        }}
      >
        {RANGE_LABELS[range]}
      </span>
    </button>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------
export function WatchlistChartBox() {
  const activeWatchSymbol = useWatchlistStore((s) => s.activeWatchSymbol);
  const activeChartRange = useWatchlistStore((s) => s.activeChartRange);
  const setActiveWatchSymbol = useWatchlistStore((s) => s.setActiveWatchSymbol);
  const setActiveChartRange = useWatchlistStore((s) => s.setActiveChartRange);
  const hasWatchlistSymbol = useWatchlistStore((s) => s.hasWatchlistSymbol);
  const addWatchlistSymbol = useWatchlistStore((s) => s.addWatchlistSymbol);

  const [detail, setDetail] = useState<TickerDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [primarySeries, setPrimarySeries] = useState<ChartSeries | null>(null);
  const [primaryLoading, setPrimaryLoading] = useState(false);
  const [primaryError, setPrimaryError] = useState<string | null>(null);
  const [showExtended, setShowExtended] = useState(true);
  const [thumbnailSeries, setThumbnailSeries] = useState<
    Partial<Record<ChartRange, ChartSeries>>
  >({});

  // Per-(symbol,range) cache so switching back to a tab doesn't re-fetch
  const cache = useRef<Map<string, ChartSeries>>(new Map());

  // Reset state when symbol changes
  useEffect(() => {
    if (!activeWatchSymbol) return;
    setDetail(null);
    setPrimarySeries(null);
    setPrimaryError(null);
    setThumbnailSeries({});
    setShowExtended(true);
    // Reset range to live when opening a new symbol
    setActiveChartRange("live");
  }, [activeWatchSymbol, setActiveChartRange]);

  // Fetch ticker detail on symbol change
  useEffect(() => {
    if (!activeWatchSymbol) return;
    setDetailLoading(true);
    fetchTickerDetail(activeWatchSymbol)
      .then((d) => setDetail(d))
      .catch(() => setDetail(null))
      .finally(() => setDetailLoading(false));
  }, [activeWatchSymbol]);

  // Fetch primary chart on symbol or range change
  const fetchPrimary = useCallback(
    (sym: string, range: ChartRange) => {
      const key = cacheKey(sym, range);
      const cached = cache.current.get(key);
      if (cached) {
        setPrimarySeries(cached);
        return;
      }
      setPrimaryLoading(true);
      setPrimaryError(null);
      fetchChart(sym, range)
        .then((s) => {
          cache.current.set(key, s);
          setPrimarySeries(s);
        })
        .catch((e: unknown) => {
          const msg = e instanceof Error ? e.message : "Failed to load chart";
          setPrimaryError(msg);
        })
        .finally(() => setPrimaryLoading(false));
    },
    [],
  );

  useEffect(() => {
    if (!activeWatchSymbol) return;
    fetchPrimary(activeWatchSymbol, activeChartRange);
  }, [activeWatchSymbol, activeChartRange, fetchPrimary]);

  // Eagerly fetch thumbnails after primary loads
  useEffect(() => {
    if (!activeWatchSymbol) return;
    THUMBNAIL_RANGES.forEach((r) => {
      const key = cacheKey(activeWatchSymbol, r);
      if (cache.current.has(key)) {
        setThumbnailSeries((prev) => ({
          ...prev,
          [r]: cache.current.get(key),
        }));
        return;
      }
      fetchChart(activeWatchSymbol, r)
        .then((s) => {
          cache.current.set(key, s);
          setThumbnailSeries((prev) => ({ ...prev, [r]: s }));
        })
        .catch(() => {/* silently skip failed thumbnails */});
    });
  }, [activeWatchSymbol]);

  if (!activeWatchSymbol) return null;

  const rMotion = prefersReducedMotion();
  const enterAnimation = rMotion
    ? {}
    : {
        animation: `wlcbEnter ${motion.normal}ms ${motion.easeOut} both`,
      };

  const symbol = activeWatchSymbol;
  const isSaved = hasWatchlistSymbol(symbol);

  const dayPct = detail?.day_change_pct;
  const priceStr =
    detail?.current_price != null ? `$${detail.current_price.toFixed(2)}` : null;
  const dayStr =
    dayPct != null
      ? `${dayPct >= 0 ? "+" : ""}${(dayPct * 100).toFixed(2)}%`
      : null;
  const dayColor = dayPct != null ? (dayPct >= 0 ? T.green : T.red) : T.muted;

  const handleTabSelect = (r: ChartRange) => {
    setActiveChartRange(r);
    fetchPrimary(symbol, r);
  };

  const handleThumbnailClick = (r: ChartRange) => {
    setActiveChartRange(r);
    fetchPrimary(symbol, r);
  };

  const isLive = activeChartRange === "live";

  return (
    <>
      {/* Keyframe injection (once — idempotent) */}
      <style>{`
        @keyframes wlcbEnter {
          from { opacity: 0; transform: translateY(-6px); }
          to   { opacity: 1; transform: translateY(0); }
        }
      `}</style>

      <div
        data-testid="watchlist-chart-box"
        role="dialog"
        aria-label={`${symbol} chart`}
        style={{
          position: "absolute",
          top: 16,
          right: 16,
          zIndex: 18,
          width: "min(500px, calc(100vw - 96px))",
          maxHeight: "80vh",
          background: T.bg,
          border: T.border,
          borderRadius: T.radius,
          fontFamily: T.sans,
          fontSize: 12,
          color: T.text,
          backdropFilter: "blur(8px)",
          overflowY: "auto",
          ...enterAnimation,
        }}
      >
        {/* ── Header ─────────────────────────────────────────────────── */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 12px",
            borderBottom: T.border,
            flexShrink: 0,
          }}
        >
          <span
            style={{
              fontFamily: T.mono,
              fontWeight: 700,
              fontSize: 14,
              color: T.text,
              letterSpacing: 0.4,
            }}
          >
            {symbol}
          </span>
          {!detailLoading && detail?.name && (
            <span style={{ color: T.muted, fontSize: 11, flex: 1, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              — {detail.name}
            </span>
          )}
          {detailLoading && (
            <span style={{ color: T.muted, fontSize: 11, flex: 1, fontStyle: "italic" }}>
              loading…
            </span>
          )}
          <div style={{ display: "flex", alignItems: "center", gap: 6, marginLeft: "auto", flexShrink: 0 }}>
            {priceStr && (
              <span style={{ fontFamily: T.mono, fontSize: 12, color: T.text }}>
                {priceStr}
              </span>
            )}
            {dayStr && (
              <span style={{ fontFamily: T.mono, fontSize: 11, color: dayColor }}>
                {dayStr}
              </span>
            )}
            {/* Add/saved button */}
            {isSaved ? (
              <span
                style={{
                  display: "inline-block",
                  padding: "1px 7px",
                  borderRadius: 4,
                  fontSize: 10,
                  fontWeight: 600,
                  background: "rgba(6,214,160,0.15)",
                  color: T.green,
                  letterSpacing: 0.4,
                }}
              >
                ✓ SAVED
              </span>
            ) : (
              <button
                aria-label={`Add ${symbol} to watchlist`}
                onClick={() => addWatchlistSymbol(symbol)}
                style={{
                  display: "inline-block",
                  padding: "1px 7px",
                  borderRadius: 4,
                  fontSize: 10,
                  fontWeight: 600,
                  background: "rgba(124,131,255,0.15)",
                  color: T.accent,
                  border: `1px solid rgba(124,131,255,0.4)`,
                  cursor: "pointer",
                  letterSpacing: 0.4,
                }}
              >
                + ADD
              </button>
            )}
            {/* Close */}
            <button
              data-testid="chart-box-close"
              aria-label="Close chart"
              onClick={() => setActiveWatchSymbol(null)}
              style={{
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
        </div>

        {/* ── Tab row ────────────────────────────────────────────────── */}
        <div
          data-testid="chart-range-tabs"
          role="tablist"
          aria-label="Chart timeframe"
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "8px 12px",
            borderBottom: T.border,
            flexWrap: "wrap",
          }}
        >
          {(["live", "5d", "1m", "3m", "6m"] as ChartRange[]).map((r) => (
            <RangeTab
              key={r}
              range={r}
              active={activeChartRange === r}
              onClick={() => handleTabSelect(r)}
            />
          ))}
          {/* Pre/post toggle — only meaningful for Live range */}
          <button
            aria-label="Include pre-market and after-hours data"
            aria-pressed={showExtended}
            disabled={!isLive}
            onClick={() => setShowExtended((v) => !v)}
            style={{
              marginLeft: "auto",
              display: "inline-flex",
              alignItems: "center",
              gap: 4,
              padding: "2px 8px",
              borderRadius: 4,
              border: `1px solid ${showExtended && isLive ? "rgba(124,131,255,0.4)" : "rgba(28,34,51,0.8)"}`,
              background: showExtended && isLive ? "rgba(124,131,255,0.1)" : "rgba(255,255,255,0.03)",
              color: isLive ? (showExtended ? T.accent : T.muted) : "rgba(141,153,174,0.35)",
              fontSize: 10,
              fontFamily: T.sans,
              cursor: isLive ? "pointer" : "default",
              letterSpacing: 0.4,
              fontWeight: 500,
            }}
          >
            {showExtended && isLive ? "✓" : "○"} pre/post
          </button>
        </div>

        {/* ── Primary chart area ─────────────────────────────────────── */}
        <div style={{ padding: "8px 12px 4px 12px" }}>
          {primaryLoading && (
            <div
              data-testid="chart-loading"
              style={{
                height: 200,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: T.muted,
                fontStyle: "italic",
                fontSize: 12,
              }}
            >
              loading…
            </div>
          )}
          {primaryError && !primaryLoading && (
            <div
              data-testid="chart-error"
              style={{
                height: 200,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: T.red,
                fontSize: 12,
              }}
            >
              {primaryError}
            </div>
          )}
          {primarySeries && !primaryLoading && !primaryError && (
            <>
              {!primarySeries.extended_available && isLive && (
                <div
                  style={{
                    fontSize: 10,
                    color: T.muted,
                    fontStyle: "italic",
                    marginBottom: 4,
                    textAlign: "right",
                  }}
                >
                  extended hours unavailable
                </div>
              )}
              <CandleChart
                candles={primarySeries.candles}
                showExtended={showExtended && isLive}
                height={200}
              />
            </>
          )}
        </div>

        {/* ── Thumbnail strip ────────────────────────────────────────── */}
        <div
          data-testid="thumbnail-strip"
          style={{
            display: "flex",
            gap: 6,
            padding: "4px 12px 10px 12px",
          }}
        >
          {THUMBNAIL_RANGES.map((r) => (
            <ThumbnailChart
              key={r}
              sym={symbol}
              range={r}
              series={thumbnailSeries[r] ?? null}
              active={activeChartRange === r}
              onClick={() => handleThumbnailClick(r)}
            />
          ))}
        </div>
      </div>
    </>
  );
}
