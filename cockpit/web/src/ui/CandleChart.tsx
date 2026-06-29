/**
 * CandleChart — candlestick chart component backed by lightweight-charts v5.
 *
 * v5 API note: uses `chart.addSeries(CandlestickSeries, {...})`.
 * (v4 used `chart.addCandlestickSeries({})`; that legacy call is gone here.)
 *
 * Extended-session (pre/post-market) bars are rendered in a muted accent
 * colour (#7c83ff at 55% opacity) so they are visually distinct from
 * regular-session bars.  Pass showExtended=false to filter them out entirely.
 */
import { useEffect, useRef } from "react";
import { CandlestickSeries, createChart } from "lightweight-charts";
import type { UTCTimestamp } from "lightweight-charts";
import type { Candle } from "../contract";

export interface CandleChartProps {
  candles: Candle[];
  showExtended: boolean;
  height?: number;
}

export function CandleChart({ candles, showExtended, height = 200 }: CandleChartProps) {
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;

    const chart = createChart(el, {
      width: el.clientWidth || 400,
      height,
      layout: {
        background: { color: "transparent" },
        textColor: "#8d99ae",
      },
      grid: {
        vertLines: { color: "rgba(28,34,51,0.6)" },
        horzLines: { color: "rgba(28,34,51,0.6)" },
      },
      rightPriceScale: { borderColor: "#1c2233" },
      timeScale: {
        borderColor: "#1c2233",
        timeVisible: true,
      },
    });

    const series = chart.addSeries(CandlestickSeries, {
      upColor: "#06d6a0",
      downColor: "#ef476f",
      borderVisible: false,
      wickUpColor: "#06d6a0",
      wickDownColor: "#ef476f",
    });

    const bars = showExtended
      ? candles
      : candles.filter((c) => c.session === "regular");

    series.setData(
      bars.map((c) => ({
        time: Math.floor(Date.parse(c.t) / 1000) as UTCTimestamp,
        open: c.o,
        high: c.h,
        low: c.l,
        close: c.c,
        // Extended-session bars: muted accent coloring
        ...(c.session !== "regular"
          ? {
              color: "rgba(124,131,255,0.55)",
              borderColor: "rgba(124,131,255,0.75)",
              wickColor: "rgba(124,131,255,0.55)",
            }
          : {}),
      })),
    );

    chart.timeScale().fitContent();

    return () => {
      chart.remove();
    };
  }, [candles, showExtended, height]);

  return (
    <div
      data-testid="candle-chart"
      ref={containerRef}
      style={{ width: "100%", height: `${height}px` }}
    />
  );
}
