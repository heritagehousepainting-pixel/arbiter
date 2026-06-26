import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { createRoot, type Root } from "react-dom/client";
import type { OpenOptionPosition, TickerDetail } from "../../contract";

const { mockFetchTicker } = vi.hoisted(() => ({ mockFetchTicker: vi.fn() }));
vi.mock("../../api", () => ({
  fetchTickerDetail: mockFetchTicker,
  fetchOptions: vi.fn(),
}));

import { OpenPositionsTable } from "../OptionsPanel";

function pos(o: Partial<OpenOptionPosition> = {}): OpenOptionPosition {
  return {
    id: "pos-uber", idea_id: "idea-1", underlying: "UBER",
    occ_symbol: "UBER270617C00065000", side: "call", strike: 65,
    expiry: "2027-06-17", contracts_qty: 1, entry_premium: 1998.5,
    delta_at_open: 0.75, iv_at_open: 0.44, underlying_open_price: 76.36,
    thesis_horizon_date: "2026-12-23", original_conviction: 0.43,
    open_ts: "2026-06-26T17:06:59Z", dte: 356,
    current_mid: null, unrealized_pl: null, unrealized_pl_pct: null, ...o,
  };
}

const UBER_DETAIL: TickerDetail = {
  symbol: "UBER", name: "Uber Technologies, Inc.",
  month_return_pct: 0.045, day_change_pct: 0.039, current_price: 75.09,
  as_of: "2026-06-26T00:00:00Z",
};

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  mockFetchTicker.mockReset();
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
function btnFor(label: string): HTMLButtonElement {
  return Array.from(container.querySelectorAll("button[aria-expanded]")).find(
    (b) => b.textContent?.includes(label),
  ) as HTMLButtonElement;
}
async function click(b: HTMLButtonElement) {
  await React.act(async () => { b.click(); await Promise.resolve(); });
}

describe("OpenPositionsTable expand", () => {
  it("expands a contract, fetches the underlying once, shows tracking detail", async () => {
    mockFetchTicker.mockResolvedValue(UBER_DETAIL);
    await render(<OpenPositionsTable positions={[pos()]} />);

    const b = btnFor("UBER 65C");
    expect(b.getAttribute("aria-expanded")).toBe("false");
    await click(b);
    expect(b.getAttribute("aria-expanded")).toBe("true");

    const txt = container.textContent ?? "";
    expect(txt).toContain("Uber Technologies, Inc.");  // company name
    expect(txt).toContain("75.09");                     // today price
    expect(txt).toContain("ITM by $10.09");             // call: 75.09 - 65
    expect(txt).toContain("76.36");                     // underlying at open
    expect(mockFetchTicker).toHaveBeenCalledTimes(1);
    expect(mockFetchTicker).toHaveBeenCalledWith("UBER");
  });

  it("collapses on a second click", async () => {
    mockFetchTicker.mockResolvedValue(UBER_DETAIL);
    await render(<OpenPositionsTable positions={[pos()]} />);
    const b = btnFor("UBER 65C");
    await click(b);
    expect(container.textContent).toContain("Uber Technologies, Inc.");
    await click(b);
    expect(b.getAttribute("aria-expanded")).toBe("false");
    expect(container.textContent).not.toContain("Uber Technologies, Inc.");
  });

  it("keeps only one option open at a time", async () => {
    mockFetchTicker.mockResolvedValue(UBER_DETAIL);
    await render(
      <OpenPositionsTable positions={[pos(), pos({ id: "p2", underlying: "MSFT" })]} />,
    );
    const b1 = btnFor("UBER 65C");
    const b2 = btnFor("MSFT 65C");
    await click(b1);
    expect(b1.getAttribute("aria-expanded")).toBe("true");
    await click(b2);
    expect(b1.getAttribute("aria-expanded")).toBe("false");
    expect(b2.getAttribute("aria-expanded")).toBe("true");
  });

  it("computes OTM for a put below its strike", async () => {
    mockFetchTicker.mockResolvedValue({ ...UBER_DETAIL, current_price: 70 });
    await render(<OpenPositionsTable positions={[pos({ side: "put", strike: 65 })]} />);
    await click(btnFor("UBER 65P"));
    // put: strike(65) - current(70) = -5 → OTM by 5
    expect(container.textContent).toContain("OTM by $5.00");
  });
});
