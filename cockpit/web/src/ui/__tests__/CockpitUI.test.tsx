/**
 * Vitest tests for the Lane 4 UI components.
 * Uses react-dom/client + jsdom (no @testing-library/react needed).
 */
import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createRoot, type Root } from "react-dom/client";
import { CockpitUI } from "../CockpitUI";
import { useCockpitStore } from "../store";
import type { NodeDetail, State } from "../../contract";

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const FIXTURE_STATE: State = {
  nodes: {
    "figure.pelosi": { intensity: 0.8, status: "active" },
    "trade.amzn": { intensity: 0.6, value: 1234.56 },
  },
  dynamic_nodes: [],
  dynamic_edges: [],
  account: { equity: 12345.67, daily_pl: 89.01 },
  health: { db: true, daemon: true, alpaca: false },
  kill_switch: { halted: false },
  as_of: "2026-06-22T10:00:00Z",
};

const FIXTURE_STATE_HALTED: State = {
  ...FIXTURE_STATE,
  kill_switch: { halted: true },
  account: { equity: 10000.0, daily_pl: -200.0 },
};

const FIXTURE_NODE_FIGURE: NodeDetail = {
  id: "figure.pelosi",
  type: "figure",
  label: "Nancy Pelosi",
  summary: {
    source: "congress",
    track_record_score: null,
  },
  rows: [
    {
      ticker: "NVDA",
      txn_type: "P",
      shares: 10000,
      price: 450.0,
      filing_ts: "2026-05-10T00:00:00",
    },
    {
      ticker: "AAPL",
      txn_type: "S",
      shares: 5000,
      price: 189.5,
      filing_ts: "2026-04-02T00:00:00",
    },
  ],
};

const FIXTURE_NODE_IDEA: NodeDetail = {
  id: "idea.42",
  type: "idea",
  label: "NVDA bullish thesis",
  summary: {
    thesis: "Insider congress buying ahead of AI chip export bill.",
    state: "open",
    horizon: "medium",
    outcome_alpha_bps: null,
  },
  rows: [
    { advisor_id: "a1_congress", stance_score: 0.72, confidence: 0.85 },
    { side: "buy", qty: 100, status: "filled" },
  ],
};

const FIXTURE_NODE_TRADE: NodeDetail = {
  id: "trade.nvda",
  type: "trade",
  label: "NVDA long",
  summary: {
    ticker: "NVDA",
    side: "long",
    qty: 100,
    avg_entry: 450.0,
    unrealized_pl: 1200.5,
    originating_idea: "idea.42",
    originating_figure: "Nancy Pelosi",
  },
  rows: [],
};

// ---------------------------------------------------------------------------
// Mock fetchNode so tests don't need a real API
// ---------------------------------------------------------------------------
vi.mock("../../api", () => ({
  fetchNode: vi.fn(() => Promise.resolve(null)),
  fetchGraph: vi.fn(() => Promise.resolve({ nodes: [], edges: [] })),
  fetchState: vi.fn(() => Promise.resolve(null)),
  fetchPositions: vi.fn(() =>
    Promise.resolve({
      positions: [],
      portfolio: {
        equity: 10000, cash: null, daily_pl: 0, n_open: 0, n_long: 0, n_short: 0,
        gross_exposure: 0, net_exposure: 0, total_cost_basis: 0,
        total_unrealized_pl: 0, total_unrealized_pl_pct: null,
      },
      as_of: "2026-06-22T00:00:00Z",
      alpaca_ok: true,
    }),
  ),
  fetchOptions: vi.fn(() =>
    Promise.resolve({
      options_mode: "off",
      open_positions: [],
      recent_shadow_plays: [],
      recent_outcomes: [],
      n_open: 0,
      sleeve_used_pct: null,
      win_rate: null,
      avg_option_pl_pct: null,
      avg_underlying_alpha_bps: null,
      as_of: "2026-06-26T00:00:00Z",
    }),
  ),
  fetchIvSeries: vi.fn(() =>
    Promise.resolve({ underlying: "AAPL", points: [], current_iv_rank: null, as_of: "2026-06-26T00:00:00Z" }),
  ),
  subscribeEvents: vi.fn(() => () => {}),
}));

import { fetchNode } from "../../api";

// ---------------------------------------------------------------------------
// DOM setup — one root per test, properly managed
// ---------------------------------------------------------------------------
let container: HTMLDivElement;
let root: Root;

beforeEach(() => {
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  // Reset store between tests
  useCockpitStore.setState({
    hoveredId: null,
    selectedId: null,
    focusCluster: null,
    walkthroughStep: null,
  });
  // Reset mocks
  vi.mocked(fetchNode).mockReset();
  vi.mocked(fetchNode).mockResolvedValue(null as unknown as NodeDetail);
});

afterEach(async () => {
  await React.act(async () => {
    root.unmount();
  });
  container.remove();
});

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
async function render(ui: React.ReactElement) {
  await React.act(async () => {
    root.render(ui);
  });
}

// ---------------------------------------------------------------------------
// HUD tests
// ---------------------------------------------------------------------------
describe("HUD", () => {
  it("renders equity and daily P&L from state", async () => {
    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId={null}
        onClose={() => {}}
      />
    );
    const hud = container.querySelector("[data-testid='hud']");
    expect(hud).not.toBeNull();
    expect(hud!.textContent).toContain("12345.67");
    expect(hud!.textContent).toContain("89.01");
  });

  it("shows dash when state is null", async () => {
    await render(<CockpitUI state={null} selectedId={null} onClose={() => {}} />);
    const hud = container.querySelector("[data-testid='hud']");
    expect(hud!.textContent).toContain("—");
  });

  it("renders HALTED banner when kill_switch.halted is true", async () => {
    await render(
      <CockpitUI
        state={FIXTURE_STATE_HALTED}
        selectedId={null}
        onClose={() => {}}
      />
    );
    const banner = container.querySelector("[data-testid='halted-banner']");
    expect(banner).not.toBeNull();
    expect(banner!.textContent).toContain("HALTED");
  });

  it("does NOT render HALTED banner when not halted", async () => {
    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId={null}
        onClose={() => {}}
      />
    );
    const banner = container.querySelector("[data-testid='halted-banner']");
    expect(banner).toBeNull();
  });

  it("shows negative P&L when daily_pl is negative", async () => {
    await render(
      <CockpitUI
        state={FIXTURE_STATE_HALTED}
        selectedId={null}
        onClose={() => {}}
      />
    );
    const hud = container.querySelector("[data-testid='hud']");
    expect(hud!.textContent).toContain("-200.00");
  });
});

// ---------------------------------------------------------------------------
// Inspection Panel tests
// ---------------------------------------------------------------------------
describe("InspectionPanel", () => {
  it("does not render panel when selectedId is null", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );
    const panel = container.querySelector("[data-testid='inspection-panel']");
    expect(panel).toBeNull();
  });

  it("renders panel with figure detail", async () => {
    vi.mocked(fetchNode).mockResolvedValueOnce(FIXTURE_NODE_FIGURE);

    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId="figure.pelosi"
        onClose={() => {}}
      />
    );

    // Panel shows (loading or loaded)
    const panel = container.querySelector("[data-testid='inspection-panel']");
    expect(panel).not.toBeNull();

    // Wait for the fetchNode promise to resolve and state to update
    await React.act(async () => {
      await Promise.resolve();
    });

    expect(panel!.textContent).toContain("Nancy Pelosi");
    expect(panel!.textContent).toContain("NVDA");
    expect(panel!.textContent).toContain("congress");
  });

  it("renders idea detail with thesis", async () => {
    vi.mocked(fetchNode).mockResolvedValueOnce(FIXTURE_NODE_IDEA);

    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId="idea.42"
        onClose={() => {}}
      />
    );

    await React.act(async () => {
      await Promise.resolve();
    });

    const panel = container.querySelector("[data-testid='inspection-panel']");
    expect(panel!.textContent).toContain("NVDA bullish thesis");
    expect(panel!.textContent).toContain(
      "Insider congress buying ahead of AI chip export bill."
    );
  });

  it("renders trade detail with P&L", async () => {
    vi.mocked(fetchNode).mockResolvedValueOnce(FIXTURE_NODE_TRADE);

    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId="trade.nvda"
        onClose={() => {}}
      />
    );

    await React.act(async () => {
      await Promise.resolve();
    });

    const panel = container.querySelector("[data-testid='inspection-panel']");
    expect(panel!.textContent).toContain("NVDA long");
    expect(panel!.textContent).toContain("1200.50");
    expect(panel!.textContent).toContain("Nancy Pelosi");
  });

  it("renders error state when fetchNode rejects", async () => {
    vi.mocked(fetchNode).mockRejectedValueOnce(new Error("node not found"));

    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId="bad.id"
        onClose={() => {}}
      />
    );

    await React.act(async () => {
      await Promise.resolve();
    });

    const panel = container.querySelector("[data-testid='inspection-panel']");
    expect(panel!.textContent).toContain("node not found");
  });

  it("calls onClose when close button is clicked", async () => {
    vi.mocked(fetchNode).mockResolvedValueOnce(FIXTURE_NODE_FIGURE);
    const onClose = vi.fn();

    await render(
      <CockpitUI
        state={FIXTURE_STATE}
        selectedId="figure.pelosi"
        onClose={onClose}
      />
    );

    await React.act(async () => {
      await Promise.resolve();
    });

    const closeBtn = container.querySelector(
      "[data-testid='inspection-panel'] button[aria-label='Close panel']"
    ) as HTMLButtonElement | null;
    expect(closeBtn).not.toBeNull();
    await React.act(async () => {
      closeBtn!.click();
    });
    expect(onClose).toHaveBeenCalledOnce();
  });
});

// ---------------------------------------------------------------------------
// Store / hover seam tests
// ---------------------------------------------------------------------------
describe("store seam", () => {
  it("hover tooltip appears when hoveredId is set in store", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    // Before: no tooltip
    expect(
      container.querySelector("[data-testid='hover-tooltip']")
    ).toBeNull();

    // Set hoveredId via store (simulating Lane 3)
    await React.act(async () => {
      useCockpitStore.getState().setHoveredId("figure.pelosi");
    });

    const tooltip = container.querySelector("[data-testid='hover-tooltip']");
    expect(tooltip).not.toBeNull();
    expect(tooltip!.textContent).toContain("figure.pelosi");
  });

  it("hover tooltip disappears when hoveredId is cleared", async () => {
    await React.act(async () => {
      useCockpitStore.getState().setHoveredId("some.node");
    });

    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    await React.act(async () => {
      useCockpitStore.getState().setHoveredId(null);
    });

    expect(
      container.querySelector("[data-testid='hover-tooltip']")
    ).toBeNull();
  });

  it("store selectedId drives panel when prop selectedId is null", async () => {
    vi.mocked(fetchNode).mockResolvedValue(FIXTURE_NODE_FIGURE);

    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    // No panel yet
    expect(
      container.querySelector("[data-testid='inspection-panel']")
    ).toBeNull();

    // Lane 3 drives selection via store
    await React.act(async () => {
      useCockpitStore.getState().setSelectedId("figure.pelosi");
    });

    expect(
      container.querySelector("[data-testid='inspection-panel']")
    ).not.toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Walkthrough tests
// ---------------------------------------------------------------------------
describe("Walkthrough", () => {
  it("renders the Follow the Money button", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );
    const btn = container.querySelector("[data-testid='walkthrough-btn']");
    expect(btn).not.toBeNull();
    expect(btn!.textContent).toContain("Follow the Money");
  });

  it("opens walkthrough panel on button click", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    const btn = container.querySelector(
      "[data-testid='walkthrough-btn']"
    ) as HTMLButtonElement;
    await React.act(async () => {
      btn.click();
    });

    const panel = container.querySelector("[data-testid='walkthrough-panel']");
    expect(panel).not.toBeNull();
    expect(panel!.textContent).toContain("Follow the Money");
  });

  it("advances to next step", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    // Open walkthrough
    const openBtn = container.querySelector(
      "[data-testid='walkthrough-btn']"
    ) as HTMLButtonElement;
    await React.act(async () => {
      openBtn.click();
    });

    // Step 1 of 9 shown initially (9 steps: 8 original + opt.layer step)
    const panel = container.querySelector("[data-testid='walkthrough-panel']")!;
    expect(panel.textContent).toContain("1 / 9");

    // Click Next
    const nextBtn = Array.from(panel.querySelectorAll("button")).find((b) =>
      b.textContent?.includes("Next")
    ) as HTMLButtonElement;
    await React.act(async () => {
      nextBtn.click();
    });

    expect(panel.textContent).toContain("2 / 9");
  });

  it("sets walkthroughStep + focusCluster in store on start", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    const btn = container.querySelector(
      "[data-testid='walkthrough-btn']"
    ) as HTMLButtonElement;
    await React.act(async () => {
      btn.click();
    });

    const { walkthroughStep, focusCluster } = useCockpitStore.getState();
    expect(walkthroughStep).toBe(0);
    expect(focusCluster).toBe("figures");
  });
});

// ---------------------------------------------------------------------------
// Legend tests
// ---------------------------------------------------------------------------
describe("Legend", () => {
  it("legend is visible by default (legibility)", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );
    const legend = container.querySelector("[data-testid='legend']");
    expect(legend).not.toBeNull();
    expect(legend!.textContent).toContain("Tracked Figures");
    expect(legend!.textContent).toContain("Decision Core");
    expect(legend!.textContent).toContain("Cluster Colors");
  });

  it("legend toggles hidden on button click", async () => {
    await render(
      <CockpitUI state={FIXTURE_STATE} selectedId={null} onClose={() => {}} />
    );

    // Legend starts visible → its toggle button reads "Hide".
    const btn = Array.from(container.querySelectorAll("button")).find(
      (b) => b.textContent === "Hide"
    ) as HTMLButtonElement;
    expect(btn).not.toBeUndefined();

    await React.act(async () => {
      btn.click();
    });

    const legend = container.querySelector("[data-testid='legend']");
    expect(legend).toBeNull();
  });
});
