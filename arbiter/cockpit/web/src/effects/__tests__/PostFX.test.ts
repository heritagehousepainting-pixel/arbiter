// LANE 5 — PostFX / effects tests
// Note: PostFX itself is a React component that requires a WebGL context, so
// we test the surrounding logic: the reduced-motion helper, the CSS import
// side-effect (module loads without throwing), and the theme token plumbing.
import { describe, it, expect, vi, afterEach } from "vitest";
import { prefersReducedMotion } from "../../theme/theme";

// ── prefersReducedMotion helper ────────────────────────────────────────────
describe("prefersReducedMotion()", () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns false when matchMedia says no-preference", () => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: false,
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    expect(prefersReducedMotion()).toBe(false);
  });

  it("returns true when matchMedia says reduce", () => {
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: vi.fn().mockImplementation((query: string) => ({
        matches: query === "(prefers-reduced-motion: reduce)",
        media: query,
        addEventListener: vi.fn(),
        removeEventListener: vi.fn(),
        dispatchEvent: vi.fn(),
      })),
    });
    expect(prefersReducedMotion()).toBe(true);
  });

  it("calls matchMedia with the correct media query string", () => {
    const mockMatchMedia = vi.fn().mockReturnValue({
      matches: false,
      media: "",
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    });
    Object.defineProperty(window, "matchMedia", {
      writable: true,
      value: mockMatchMedia,
    });
    prefersReducedMotion();
    expect(mockMatchMedia).toHaveBeenCalledWith("(prefers-reduced-motion: reduce)");
  });
});

// ── effects/PostFX module loads cleanly ───────────────────────────────────
describe("PostFX module", () => {
  it("imports without throwing (CSS side-effect import is safe)", async () => {
    // The CSS import is a side-effect; Vite/vitest handles it as an empty module.
    // If the import throws, this test fails.
    await expect(import("../PostFX")).resolves.toBeDefined();
  });

  it("exports a PostFX function", async () => {
    const mod = await import("../PostFX");
    expect(typeof mod.PostFX).toBe("function");
  });
});

// ── CSS custom properties are consistently named ───────────────────────────
// We can't inject the CSS in test env, but we verify the token set is complete
// by checking the theme exports that the CSS mirrors.
describe("theme ↔ CSS variable consistency", () => {
  it("theme exports the tokens that CSS declares as custom properties", async () => {
    const { palette, motion } = await import("../../theme/theme");

    // These are the tokens the CSS declares — verify the JS side is present
    expect(palette.bg).toBeTruthy();
    expect(palette.bgSurface).toBeTruthy();
    expect(palette.text).toBeTruthy();
    expect(palette.ok).toBeTruthy();
    expect(palette.bad).toBeTruthy();
    expect(palette.warn).toBeTruthy();
    expect(palette.accent).toBeTruthy();
    expect(palette.muted).toBeTruthy();

    // Motion tokens
    expect(typeof motion.fast).toBe("number");
    expect(typeof motion.normal).toBe("number");
    expect(typeof motion.slow).toBe("number");
    expect(motion.easeOut).toContain("cubic-bezier");
  });
});
