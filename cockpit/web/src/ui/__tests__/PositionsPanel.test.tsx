import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { createRoot, type Root } from "react-dom/client";
import type { PositionsResponse, TickerDetail } from "../../contract";

// Fixtures
const FIXTURE: PositionsResponse = {
  positions: [
    {
      ticker: "AMZN", side: "long", qty: 1, avg_entry: 239.9, current_price: 234.16,
      market_value: 234.16, cost_basis: 239.9, unrealized_pl: -5.74,
      unrealized_pl_pct: -0.02393, day_change_pct: -0.0031,
    },
    {
      ticker: "UBER", side: "short", qty: 1, avg_entry: 72.19, current_price: 71.82,
      market_value: -71.82, cost_basis: 72.19, unrealized_pl: 0.37,
      unrealized_pl_pct: 0.00513, day_change_pct: 0.0099,
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

const AMZN_DETAIL: TickerDetail = {
  symbol: "AMZN",
  name: "Amazon.com Inc.",
  month_return_pct: 0.0423,
  current_price: 234.16,
  as_of: "2026-06-26T00:00:00Z",
};

const UBER_DETAIL: TickerDetail = {
  symbol: "UBER",
  name: "Uber Technologies Inc.",
  month_return_pct: -0.0152,
  current_price: 71.82,
  as_of: "2026-06-26T00:00:00Z",
};

// Mock both fetchPositions and fetchTickerDetail.
// Use vi.hoisted so the mock variable is available inside the hoisted vi.mock factory.
const { mockFetchTicker } = vi.hoisted(() => ({
  mockFetchTicker: vi.fn(),
}));

vi.mock("../../api", () => ({
  fetchPositions: vi.fn(() => Promise.resolve(FIXTURE)),
  fetchTickerDetail: mockFetchTicker,
}));

import { PositionsPanel } from "../PositionsPanel";

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  // Default: fetchTickerDetail resolves with AMZN_DETAIL or UBER_DETAIL
  mockFetchTicker.mockImplementation((sym: string) =>
    sym === "AMZN"
      ? Promise.resolve(AMZN_DETAIL)
      : Promise.resolve(UBER_DETAIL),
  );
});

afterEach(() => {
  React.act(() => root.unmount());
  container.remove();
  vi.clearAllMocks();
});

async function render(el: React.ReactElement) {
  await React.act(async () => { root.render(el); });
  await React.act(async () => { await Promise.resolve(); });
}

describe("PositionsPanel", () => {
  // ---- existing behavior preserved ----------------------------------------

  it("renders existing columns: cost/share, ROI and P&L", async () => {
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
    expect(txt).toContain("1L / 1S");
    expect(txt).toContain("9994.64");
  });

  // ---- accordion toggle ---------------------------------------------------

  it("ticker cell renders as a button with aria-expanded=false initially", async () => {
    await render(<PositionsPanel />);
    const buttons = container.querySelectorAll("button[aria-expanded]");
    // At least the two ticker buttons
    const tickerButtons = Array.from(buttons).filter(
      (b) => b.textContent?.includes("AMZN") || b.textContent?.includes("UBER"),
    );
    expect(tickerButtons.length).toBeGreaterThanOrEqual(2);
    tickerButtons.forEach((b) => {
      expect(b.getAttribute("aria-expanded")).toBe("false");
    });
  });

  it("clicking ticker button sets aria-expanded=true and shows detail", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;
    expect(amznBtn).toBeTruthy();

    await React.act(async () => {
      amznBtn.click();
      await Promise.resolve();
    });

    expect(amznBtn.getAttribute("aria-expanded")).toBe("true");
  });

  it("expanded row shows company name from detail", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => {
      amznBtn.click();
      await Promise.resolve();  // flush fetchTickerDetail
    });
    // flush the detail promise
    await React.act(async () => { await Promise.resolve(); });

    expect(container.textContent).toContain("Amazon.com Inc.");
  });

  it("expanded row shows month return pct formatted as percent", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => {
      amznBtn.click();
      await Promise.resolve();
    });
    await React.act(async () => { await Promise.resolve(); });

    // +4.23% for month_return_pct = 0.0423
    expect(container.textContent).toMatch(/\+4\.23%/);
  });

  it("clicking same ticker again collapses the row", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });
    expect(amznBtn.getAttribute("aria-expanded")).toBe("true");

    await React.act(async () => { amznBtn.click(); });
    expect(amznBtn.getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).not.toContain("Amazon.com Inc.");
  });

  it("only one ticker can be open at a time", async () => {
    await render(<PositionsPanel />);
    const buttons = Array.from(container.querySelectorAll("button[aria-expanded]")).filter(
      (b) => b.textContent?.includes("AMZN") || b.textContent?.includes("UBER"),
    );
    const [amznBtn, uberBtn] = buttons as HTMLButtonElement[];

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });
    expect(amznBtn.getAttribute("aria-expanded")).toBe("true");

    await React.act(async () => { uberBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });

    expect(amznBtn.getAttribute("aria-expanded")).toBe("false");
    expect(uberBtn.getAttribute("aria-expanded")).toBe("true");
    // AMZN detail hidden, UBER detail shown
    expect(container.textContent).not.toContain("Amazon.com Inc.");
    expect(container.textContent).toContain("Uber Technologies Inc.");
  });

  // ---- loading state ------------------------------------------------------

  it("shows loading while fetchTickerDetail is in flight", async () => {
    let resolveDetail!: (v: TickerDetail) => void;
    const pending = new Promise<TickerDetail>((res) => { resolveDetail = res; });
    mockFetchTicker.mockReturnValueOnce(pending);

    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); });
    // Detail not resolved yet → loading state
    expect(container.textContent).toContain("loading");

    await React.act(async () => { resolveDetail(AMZN_DETAIL); await Promise.resolve(); });
    expect(container.textContent).not.toContain("loading");
    expect(container.textContent).toContain("Amazon.com Inc.");
  });

  it("rapid switch: A loading, click B, A resolves → B still shows loading until B resolves", async () => {
    // AMZN fetch stays pending; UBER fetch stays pending (controlled resolvers)
    let resolveAmzn!: (v: TickerDetail) => void;
    let resolveUber!: (v: TickerDetail) => void;
    const amznPending = new Promise<TickerDetail>((res) => { resolveAmzn = res; });
    const uberPending = new Promise<TickerDetail>((res) => { resolveUber = res; });
    mockFetchTicker.mockImplementation((sym: string) =>
      sym === "AMZN" ? amznPending : uberPending,
    );

    await render(<PositionsPanel />);
    const buttons = Array.from(container.querySelectorAll("button[aria-expanded]")).filter(
      (b) => b.textContent?.includes("AMZN") || b.textContent?.includes("UBER"),
    );
    const amznBtn = buttons.find((b) => b.textContent?.includes("AMZN")) as HTMLButtonElement;
    const uberBtn = buttons.find((b) => b.textContent?.includes("UBER")) as HTMLButtonElement;

    // Expand A → loading
    await React.act(async () => { amznBtn.click(); });
    expect(container.textContent).toContain("loading");

    // Switch to B (A still in flight) → B now open + loading
    await React.act(async () => { uberBtn.click(); });
    expect(uberBtn.getAttribute("aria-expanded")).toBe("true");
    expect(container.textContent).toContain("loading");

    // A resolves while B is open → must NOT clear B's loading
    await React.act(async () => { resolveAmzn(AMZN_DETAIL); await Promise.resolve(); });
    expect(container.textContent).toContain("loading");
    expect(container.textContent).not.toContain("Uber Technologies Inc.");

    // B resolves → B detail shows, loading gone
    await React.act(async () => { resolveUber(UBER_DETAIL); await Promise.resolve(); });
    expect(container.textContent).not.toContain("loading");
    expect(container.textContent).toContain("Uber Technologies Inc.");
  });

  it("fetchTickerDetail fires exactly once per single expand (no StrictMode double-fetch)", async () => {
    // Side effects are hoisted out of the setOpenTicker updater, so a single
    // click triggers exactly one fetch (the updater is pure).
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });

    const amznCalls = mockFetchTicker.mock.calls.filter(
      (args: unknown[]) => args[0] === "AMZN",
    );
    expect(amznCalls.length).toBe(1);
  });

  // ---- null / "—" state ---------------------------------------------------

  it("null name renders as em-dash", async () => {
    mockFetchTicker.mockResolvedValueOnce({
      symbol: "AMZN", name: null, month_return_pct: null,
      current_price: null, as_of: "2026-06-26T00:00:00Z",
    } satisfies TickerDetail);

    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });

    // The panel should render "—" for null name
    expect(container.textContent).toMatch(/—/);
  });

  // ---- session cache ------------------------------------------------------

  it("fetchTickerDetail called only once per symbol across multiple expands", async () => {
    await render(<PositionsPanel />);
    const amznBtn = Array.from(container.querySelectorAll("button[aria-expanded]")).find(
      (b) => b.textContent?.includes("AMZN"),
    ) as HTMLButtonElement;

    // expand → collapse → expand
    await React.act(async () => { amznBtn.click(); await Promise.resolve(); });
    await React.act(async () => { await Promise.resolve(); });
    await React.act(async () => { amznBtn.click(); }); // collapse
    await React.act(async () => { amznBtn.click(); await Promise.resolve(); }); // re-expand
    await React.act(async () => { await Promise.resolve(); });

    const amznCalls = mockFetchTicker.mock.calls.filter(
      (args: unknown[]) => args[0] === "AMZN",
    );
    expect(amznCalls.length).toBe(1);  // cached after first fetch
  });
});
