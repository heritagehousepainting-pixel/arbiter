/**
 * Vitest tests for RoboticsPanel — the display-only robotics board overlay.
 *
 * Mirrors the WatchlistBar.test harness (react-dom createRoot + React.act,
 * vi.mock of ../../api, matchMedia shim).
 */
import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { RoboticsPanel } from "../RoboticsPanel";
import { useWatchlistStore } from "../watchlistStore";

vi.mock("../../api", () => ({
  fetchRoboticsWatchlist: vi.fn(),
  fetchRoboticsSignals: vi.fn(),
  fetchTickerDetail: vi.fn(),
  fetchChart: vi.fn(),
}));
import { fetchRoboticsSignals, fetchRoboticsWatchlist } from "../../api";

beforeAll(() => {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((q: string) => ({
      matches: false, media: q, addEventListener: vi.fn(), removeEventListener: vi.fn(),
    })),
  });
});

let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  useWatchlistStore.setState({ activeWatchSymbol: null });
  vi.mocked(fetchRoboticsWatchlist).mockReset();
  vi.mocked(fetchRoboticsSignals).mockReset();
  // default: empty signals feed (individual tests override)
  vi.mocked(fetchRoboticsSignals).mockResolvedValue({
    signals: [], as_of: "2026-07-13T00:00:00Z",
  } as never);
});

afterEach(async () => {
  await React.act(async () => { root.unmount(); });
  container.remove();
});

async function render(ui: React.ReactElement) {
  await React.act(async () => { root.render(ui); });
}

async function flush() {
  await React.act(async () => { await Promise.resolve(); await Promise.resolve(); });
}

const ROSTER = {
  generated: "2026-07-13",
  entries: [
    {
      symbol: "NVDA", company: "Nvidia", layer: "compute", longevity: "chokepoint",
      priceable: true, form_factors: ["all"], early_insight: false, trigger: null,
      region: "US", note: "socket",
    },
    {
      symbol: "6324.T", company: "Harmonic Drive Systems", layer: "components",
      longevity: "chokepoint", priceable: false, form_factors: ["humanoid"],
      early_insight: true, trigger: "Optimus ramp", region: "Japan", note: "reducer",
    },
  ],
};

describe("RoboticsPanel", () => {
  it("collapsed by default (icon only)", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    expect(container.querySelector("[data-testid='robotics-icon-btn']")).not.toBeNull();
    expect(container.querySelector("[data-testid='robotics-panel-expanded']")).toBeNull();
  });

  it("expands and groups rows by layer after fetch", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await flush();
    const panel = container.querySelector("[data-testid='robotics-panel-expanded']");
    expect(panel).not.toBeNull();
    expect(panel?.textContent).toContain("Nvidia");
    expect(panel?.textContent).toContain("Harmonic Drive Systems");
    expect(panel?.textContent).toContain("COMPUTE");
    expect(panel?.textContent).toContain("COMPONENTS");
  });

  it("priceable row click sets activeWatchSymbol; reference row has no chart button", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await flush();
    const nvda = container.querySelector("[data-testid='robotics-row-NVDA'] button");
    expect(nvda).not.toBeNull();
    await React.act(async () => { (nvda as HTMLButtonElement).click(); });
    expect(useWatchlistStore.getState().activeWatchSymbol).toBe("NVDA");
    // reference row has no chart-opening button
    expect(container.querySelector("[data-testid='robotics-row-6324.T'] button")).toBeNull();
  });

  it("early-insight row shows the star and trigger", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await flush();
    const row = container.querySelector("[data-testid='robotics-row-6324.T']");
    expect(row?.textContent).toContain("★");
    expect(row?.textContent).toContain("Optimus ramp");
  });

  it("auto-collapses when inspectionOpen becomes true", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await flush();
    expect(container.querySelector("[data-testid='robotics-panel-expanded']")).not.toBeNull();
    await render(<RoboticsPanel inspectionOpen={true} />);
    expect(container.querySelector("[data-testid='robotics-panel-expanded']")).toBeNull();
    expect(container.querySelector("[data-testid='robotics-icon-btn']")).not.toBeNull();
  });

  it("renders the recent-signals feed with trigger-hit markers", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    vi.mocked(fetchRoboticsSignals).mockResolvedValue({
      signals: [
        { as_of: "2026-07-13T08:00:00Z", headline: "Bosch → Neura production order",
          summary: "s", category: "integrator", symbols: ["NEURA"],
          trigger_hit: true, trigger_name: "NEURA", sources: [] },
        { as_of: "2026-07-13T08:00:00Z", headline: "Nvidia ships Thor", summary: "s",
          category: "compute", symbols: ["NVDA"], trigger_hit: false,
          trigger_name: null, sources: [] },
      ],
      as_of: "2026-07-13T08:00:00Z",
    } as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await flush();
    const feed = container.querySelector("[data-testid='robotics-signals']");
    expect(feed).not.toBeNull();
    expect(feed?.textContent).toContain("RECENT SIGNALS");
    expect(feed?.textContent).toContain("Bosch → Neura production order");
    expect(feed?.textContent).toContain("★"); // trigger-hit marker
    expect(container.querySelectorAll("[data-testid='robotics-signal']").length).toBe(2);
  });

  it("shows no feed when there are no signals", async () => {
    vi.mocked(fetchRoboticsWatchlist).mockResolvedValue(ROSTER as never);
    await render(<RoboticsPanel inspectionOpen={false} />);
    await React.act(async () => {
      (container.querySelector("[data-testid='robotics-icon-btn']") as HTMLButtonElement).click();
    });
    await flush();
    expect(container.querySelector("[data-testid='robotics-signals']")).toBeNull();
  });
});
