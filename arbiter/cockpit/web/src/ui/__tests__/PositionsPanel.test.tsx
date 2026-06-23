import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { createRoot, type Root } from "react-dom/client";
import type { PositionsResponse } from "../../contract";

const FIXTURE: PositionsResponse = {
  positions: [
    {
      ticker: "AMZN", side: "long", qty: 1, avg_entry: 239.9, current_price: 234.16,
      market_value: 234.16, cost_basis: 239.9, unrealized_pl: -5.74, unrealized_pl_pct: -0.02393,
    },
    {
      ticker: "UBER", side: "short", qty: 1, avg_entry: 72.19, current_price: 71.82,
      market_value: -71.82, cost_basis: 72.19, unrealized_pl: 0.37, unrealized_pl_pct: 0.00513,
    },
  ],
  portfolio: {
    equity: 9994.64, cash: null, daily_pl: -5.37, n_open: 2, n_long: 1, n_short: 1,
    gross_exposure: 305.98, net_exposure: 162.34, total_cost_basis: 312.09,
    total_unrealized_pl: -5.37, total_unrealized_pl_pct: -0.0172,
  },
  as_of: "2026-06-22T00:00:00Z",
  alpaca_ok: true,
};

vi.mock("../../api", () => ({
  fetchPositions: vi.fn(() => Promise.resolve(FIXTURE)),
}));

import { PositionsPanel } from "../PositionsPanel";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  React.act(() => root.unmount());
  container.remove();
});

async function render(el: React.ReactElement) {
  await React.act(async () => {
    root.render(el);
  });
  // flush the fetchPositions promise
  await React.act(async () => { await Promise.resolve(); });
}

describe("PositionsPanel", () => {
  it("renders open positions with cost/share, ROI and P&L", async () => {
    await render(<PositionsPanel />);
    const txt = container.textContent ?? "";
    expect(txt).toContain("AMZN");
    expect(txt).toContain("UBER");
    expect(txt).toContain("239.90");   // cost/share
    expect(txt).toContain("-2.39%");   // AMZN ROI
    expect(txt).toContain("+0.51%");   // UBER ROI (short in profit)
  });

  it("renders portfolio summary stats", async () => {
    await render(<PositionsPanel />);
    const txt = container.textContent ?? "";
    expect(txt).toContain("1L / 1S");   // long/short counts
    expect(txt).toContain("9994.64");   // equity
  });
});
