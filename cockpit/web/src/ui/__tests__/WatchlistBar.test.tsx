/**
 * Vitest tests for WatchlistBar.
 *
 * Covers:
 *   - Collapsed by default (icon button only; no search input)
 *   - Clicking the icon expands the search panel
 *   - Adding a valid symbol (fetchTickerDetail → name set) calls
 *     addWatchlistSymbol uppercased and renders a chip
 *   - Auto-collapses when inspectionOpen becomes true
 *
 * jsdom has no canvas → vi.mock("lightweight-charts") so CandleChart's
 * createChart call is a no-op.
 */
import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { WatchlistBar } from "../WatchlistBar";
import { useWatchlistStore } from "../watchlistStore";

// ---------------------------------------------------------------------------
// Stub lightweight-charts (jsdom has no canvas)
// ---------------------------------------------------------------------------
vi.mock("lightweight-charts", () => ({
  createChart: vi.fn(() => ({
    addSeries: vi.fn(() => ({ setData: vi.fn() })),
    remove: vi.fn(),
    timeScale: vi.fn(() => ({ fitContent: vi.fn() })),
  })),
  CandlestickSeries: { name: "Candlestick" },
}));

// ---------------------------------------------------------------------------
// Mock ../../api (test files are in src/ui/__tests__/, api is at src/api.ts)
// ---------------------------------------------------------------------------
vi.mock("../../api", () => ({
  fetchTickerDetail: vi.fn(),
  fetchChart: vi.fn(),
  fetchNode: vi.fn(),
  fetchGraph: vi.fn(),
  fetchState: vi.fn(),
  fetchPositions: vi.fn(),
  fetchOptions: vi.fn(),
  fetchIvSeries: vi.fn(),
  subscribeEvents: vi.fn(() => () => {}),
}));

import { fetchTickerDetail } from "../../api";
import type { TickerDetail } from "../../contract";

// ---------------------------------------------------------------------------
// jsdom shim: window.matchMedia is not implemented in jsdom.
// prefersReducedMotion() in theme.ts calls it; stub it out so components render.
// ---------------------------------------------------------------------------
beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: false,
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    })),
  });
});

// ---------------------------------------------------------------------------
// DOM helpers
// ---------------------------------------------------------------------------
let container: HTMLDivElement;
let root: Root;

function resetStore() {
  localStorage.clear();
  useWatchlistStore.setState({
    watchlistSymbols: [],
    activeWatchSymbol: null,
    activeChartRange: "1m",
  });
}

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  resetStore();
  vi.mocked(fetchTickerDetail).mockReset();
});

afterEach(async () => {
  await React.act(async () => {
    root.unmount();
  });
  container.remove();
});

async function render(ui: React.ReactElement) {
  await React.act(async () => {
    root.render(ui);
  });
}

/** Fire a controlled-input change on an HTMLInputElement. */
function fireChange(input: HTMLInputElement, value: string) {
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    "value",
  )?.set;
  setter?.call(input, value);
  input.dispatchEvent(new Event("input", { bubbles: true }));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("WatchlistBar — collapsed state", () => {
  it("renders only the icon button by default", async () => {
    await render(<WatchlistBar inspectionOpen={false} />);

    const iconBtn = container.querySelector("[data-testid='watchlist-icon-btn']");
    const expanded = container.querySelector("[data-testid='watchlist-bar-expanded']");

    expect(iconBtn).not.toBeNull();
    expect(expanded).toBeNull();
  });

  it("icon button has correct aria-label when collapsed", async () => {
    await render(<WatchlistBar inspectionOpen={false} />);
    const iconBtn = container.querySelector("[data-testid='watchlist-icon-btn']");
    expect(iconBtn?.getAttribute("aria-label")).toBe("Open watchlist search");
  });
});

describe("WatchlistBar — expand / collapse", () => {
  it("expands when the icon button is clicked", async () => {
    await render(<WatchlistBar inspectionOpen={false} />);

    const iconBtn = container.querySelector(
      "[data-testid='watchlist-icon-btn']",
    ) as HTMLButtonElement;

    await React.act(async () => {
      iconBtn.click();
    });

    const expanded = container.querySelector("[data-testid='watchlist-bar-expanded']");
    expect(expanded).not.toBeNull();
    // Icon button should no longer be present
    expect(container.querySelector("[data-testid='watchlist-icon-btn']")).toBeNull();
  });

  it("collapses again when the close ✕ in the header is clicked", async () => {
    await render(<WatchlistBar inspectionOpen={false} />);

    const iconBtn = container.querySelector(
      "[data-testid='watchlist-icon-btn']",
    ) as HTMLButtonElement;
    await React.act(async () => { iconBtn.click(); });

    const closeBtn = Array.from(container.querySelectorAll("button")).find(
      (b) => b.getAttribute("aria-label") === "Close watchlist search",
    ) as HTMLButtonElement;
    expect(closeBtn).not.toBeUndefined();

    await React.act(async () => { closeBtn.click(); });

    expect(container.querySelector("[data-testid='watchlist-bar-expanded']")).toBeNull();
    expect(container.querySelector("[data-testid='watchlist-icon-btn']")).not.toBeNull();
  });
});

describe("WatchlistBar — inspectionOpen auto-collapse", () => {
  it("auto-collapses when inspectionOpen prop becomes true", async () => {
    // Start expanded
    await render(<WatchlistBar inspectionOpen={false} />);
    const iconBtn = container.querySelector(
      "[data-testid='watchlist-icon-btn']",
    ) as HTMLButtonElement;
    await React.act(async () => { iconBtn.click(); });
    expect(container.querySelector("[data-testid='watchlist-bar-expanded']")).not.toBeNull();

    // Re-render with inspectionOpen=true
    await render(<WatchlistBar inspectionOpen={true} />);
    expect(container.querySelector("[data-testid='watchlist-bar-expanded']")).toBeNull();
    expect(container.querySelector("[data-testid='watchlist-icon-btn']")).not.toBeNull();
  });

  it("stays collapsed when inspectionOpen is already true on mount", async () => {
    await render(<WatchlistBar inspectionOpen={true} />);
    expect(container.querySelector("[data-testid='watchlist-icon-btn']")).not.toBeNull();
    expect(container.querySelector("[data-testid='watchlist-bar-expanded']")).toBeNull();
  });
});

describe("WatchlistBar — adding a ticker (valid symbol, name set)", () => {
  it("calls addWatchlistSymbol uppercased and renders a chip", async () => {
    vi.mocked(fetchTickerDetail).mockResolvedValueOnce({
      symbol: "AAPL",
      name: "Apple Inc.",
      current_price: 182.4,
      day_change_pct: 0.012,
      month_return_pct: null,
      as_of: "2026-06-29T00:00:00Z",
    } satisfies TickerDetail);

    await render(<WatchlistBar inspectionOpen={false} />);

    // Expand
    await React.act(async () => {
      (container.querySelector("[data-testid='watchlist-icon-btn']") as HTMLButtonElement).click();
    });

    // Fill input and press Enter
    const input = container.querySelector(
      "[data-testid='watchlist-search-input']",
    ) as HTMLInputElement;

    await React.act(async () => {
      fireChange(input, "aapl"); // lower-case to test uppercasing
    });
    await React.act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });

    // Wait for async fetchTickerDetail to resolve
    await React.act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    // fetchTickerDetail called with uppercased sym
    expect(fetchTickerDetail).toHaveBeenCalledWith("AAPL");
    // Symbol should be in the store
    expect(useWatchlistStore.getState().watchlistSymbols).toContain("AAPL");
    // Chip should be visible
    const chips = container.querySelector("[data-testid='watchlist-chips']");
    expect(chips).not.toBeNull();
    expect(chips?.textContent).toContain("AAPL");
  });

  it("shows 'add anyway' prompt for unknown ticker (name null)", async () => {
    vi.mocked(fetchTickerDetail).mockResolvedValueOnce({
      symbol: "FAKE",
      name: null,
      current_price: null,
      day_change_pct: null,
      month_return_pct: null,
      as_of: "2026-06-29T00:00:00Z",
    } satisfies TickerDetail);

    await render(<WatchlistBar inspectionOpen={false} />);

    await React.act(async () => {
      (container.querySelector("[data-testid='watchlist-icon-btn']") as HTMLButtonElement).click();
    });

    const input = container.querySelector(
      "[data-testid='watchlist-search-input']",
    ) as HTMLInputElement;

    await React.act(async () => {
      fireChange(input, "FAKE");
    });
    await React.act(async () => {
      input.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });

    await React.act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const prompt = container.querySelector("[data-testid='watchlist-unknown-prompt']");
    expect(prompt).not.toBeNull();
    expect(prompt?.textContent).toContain("unknown ticker");

    // Clicking "add anyway" should add to store
    const addAnywayBtn = container.querySelector(
      "[data-testid='watchlist-add-anyway']",
    ) as HTMLButtonElement;
    await React.act(async () => { addAnywayBtn.click(); });

    expect(useWatchlistStore.getState().watchlistSymbols).toContain("FAKE");
  });
});

describe("WatchlistBar — chip interaction", () => {
  it("clicking a chip calls setActiveWatchSymbol", async () => {
    useWatchlistStore.setState({ watchlistSymbols: ["NVDA"], activeWatchSymbol: null });

    await render(<WatchlistBar inspectionOpen={false} />);

    await React.act(async () => {
      (container.querySelector("[data-testid='watchlist-icon-btn']") as HTMLButtonElement).click();
    });

    const chipBtn = container.querySelector(
      "[aria-label='View chart for NVDA']",
    ) as HTMLButtonElement;
    expect(chipBtn).not.toBeNull();

    await React.act(async () => { chipBtn.click(); });

    expect(useWatchlistStore.getState().activeWatchSymbol).toBe("NVDA");
  });

  it("clicking remove on a chip calls removeWatchlistSymbol", async () => {
    useWatchlistStore.setState({ watchlistSymbols: ["AAPL", "MSFT"], activeWatchSymbol: null });

    await render(<WatchlistBar inspectionOpen={false} />);

    await React.act(async () => {
      (container.querySelector("[data-testid='watchlist-icon-btn']") as HTMLButtonElement).click();
    });

    const removeBtn = container.querySelector(
      "[aria-label='Remove AAPL from watchlist']",
    ) as HTMLButtonElement;
    expect(removeBtn).not.toBeNull();

    await React.act(async () => { removeBtn.click(); });

    expect(useWatchlistStore.getState().watchlistSymbols).not.toContain("AAPL");
    expect(useWatchlistStore.getState().watchlistSymbols).toContain("MSFT");
  });
});
