// LANE 5 — theme token tests
import { describe, it, expect } from "vitest";
import {
  theme,
  palette,
  glow,
  motion,
  type as typeScale,
  CLUSTER_ACCENT,
  GLOW_MATERIAL_PROPS,
  clusterGlow,
  elevation,
} from "../theme";
import type { Cluster } from "../../contract";

// ── Palette completeness ───────────────────────────────────────────────────
describe("palette", () => {
  it("exports a valid hex bg color", () => {
    expect(palette.bg).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it("exports all required base tokens", () => {
    const required = ["bg", "text", "muted", "ok", "bad", "warn", "accent"] as const;
    for (const key of required) {
      expect(palette[key], `palette.${key} missing`).toBeTruthy();
      expect(palette[key]).toMatch(/^#[0-9a-f]{3,8}$/i);
    }
  });

  it("bg matches theme.bg (App.tsx reads theme.bg)", () => {
    expect(theme.bg).toBe(palette.bg);
    expect(theme.bg).toBe("#05060a");
  });

  it("ok and bad are distinct colors", () => {
    expect(palette.ok).not.toBe(palette.bad);
  });
});

// ── Theme root ─────────────────────────────────────────────────────────────
describe("theme root", () => {
  it("has flat text and muted tokens", () => {
    expect(theme.text).toBe(palette.text);
    expect(theme.muted).toBe(palette.muted);
  });

  it("exposes sub-objects: palette, elevation, glow, motion, type, cluster", () => {
    expect(theme.palette).toBe(palette);
    expect(theme.elevation).toBe(elevation);
    expect(theme.glow).toBe(glow);
    expect(theme.motion).toBe(motion);
    expect(theme.type).toBe(typeScale);
    expect(theme.cluster).toBe(CLUSTER_ACCENT);
  });
});

// ── Glow tokens ────────────────────────────────────────────────────────────
describe("glow tokens", () => {
  it("bloom threshold is 0..1", () => {
    expect(glow.bloomThreshold).toBeGreaterThanOrEqual(0);
    expect(glow.bloomThreshold).toBeLessThanOrEqual(1);
  });

  it("bloom intensity is positive", () => {
    expect(glow.bloomIntensity).toBeGreaterThan(0);
  });

  it("bloom radius is 0..1", () => {
    expect(glow.bloomRadius).toBeGreaterThanOrEqual(0);
    expect(glow.bloomRadius).toBeLessThanOrEqual(1);
  });

  it("vignette darkness is 0..1", () => {
    expect(glow.vignetteDarkness).toBeGreaterThanOrEqual(0);
    expect(glow.vignetteDarkness).toBeLessThanOrEqual(1);
  });

  it("peak emissive > base emissive", () => {
    expect(glow.peakEmissive).toBeGreaterThan(glow.baseEmissive);
  });

  it("chroma offset is small (< 0.01) to stay tasteful", () => {
    expect(glow.chromaOffset).toBeLessThan(0.01);
  });
});

// ── Motion tokens ──────────────────────────────────────────────────────────
describe("motion tokens", () => {
  it("all duration tokens are positive numbers", () => {
    const durs = [motion.instant, motion.fast, motion.normal, motion.slow, motion.xslow];
    for (const d of durs) {
      expect(typeof d).toBe("number");
      expect(d).toBeGreaterThan(0);
    }
  });

  it("durations are ordered ascending", () => {
    expect(motion.instant).toBeLessThan(motion.fast);
    expect(motion.fast).toBeLessThan(motion.normal);
    expect(motion.normal).toBeLessThan(motion.slow);
    expect(motion.slow).toBeLessThan(motion.xslow);
  });

  it("easing strings are cubic-bezier or linear", () => {
    const easings = [motion.easeOut, motion.easeIn, motion.easeInOut, motion.spring];
    for (const e of easings) {
      expect(e).toMatch(/cubic-bezier|linear/);
    }
  });
});

// ── Type scale ─────────────────────────────────────────────────────────────
describe("type scale", () => {
  it("exports font family strings", () => {
    expect(typeScale.mono).toContain("monospace");
    expect(typeScale.sans).toContain("sans-serif");
  });

  it("exports size tokens as rem strings", () => {
    expect(typeScale.base).toMatch(/rem$/);
    expect(typeScale.lg).toMatch(/rem$/);
  });

  it("exports numeric weight tokens", () => {
    expect(typeof typeScale.normal).toBe("number");
    expect(typeof typeScale.bold).toBe("number");
    expect(typeScale.bold).toBeGreaterThan(typeScale.normal);
  });
});

// ── CLUSTER_ACCENT ─────────────────────────────────────────────────────────
describe("CLUSTER_ACCENT", () => {
  const clusters: Cluster[] = [
    "sources", "figures", "council", "core", "ideas",
    "execution", "market", "learning", "infra",
  ];

  it("covers all 9 clusters from contract.ts", () => {
    for (const c of clusters) {
      expect(CLUSTER_ACCENT[c], `CLUSTER_ACCENT.${c} missing`).toBeDefined();
    }
  });

  it("each entry has base, glow, dim, emissive", () => {
    for (const c of clusters) {
      const entry = CLUSTER_ACCENT[c];
      expect(entry.base).toMatch(/^#[0-9a-f]{3,8}$/i);
      expect(entry.glow).toBeDefined();
      expect(entry.dim).toBeDefined();
      expect(typeof entry.emissive).toBe("number");
      expect(entry.emissive).toBeGreaterThan(0);
      expect(entry.emissive).toBeLessThanOrEqual(1);
    }
  });

  it("core cluster has highest emissive (brightest region)", () => {
    const coreEmissive = CLUSTER_ACCENT.core.emissive;
    const otherEmissives = clusters
      .filter((c) => c !== "core")
      .map((c) => CLUSTER_ACCENT[c].emissive);
    for (const e of otherEmissives) {
      expect(coreEmissive).toBeGreaterThanOrEqual(e);
    }
  });
});

// ── GLOW_MATERIAL_PROPS helper ─────────────────────────────────────────────
describe("GLOW_MATERIAL_PROPS", () => {
  it("returns the right shape for mesh material spread", () => {
    const props = GLOW_MATERIAL_PROPS("#ef476f", 0.5);
    expect(props.color).toBe("#ef476f");
    expect(props.emissive).toBe("#ef476f");
    expect(typeof props.emissiveIntensity).toBe("number");
    expect(props.roughness).toBeGreaterThanOrEqual(0);
    expect(props.metalness).toBeGreaterThanOrEqual(0);
  });

  it("emissiveIntensity at intensity=0 equals glow.baseEmissive", () => {
    const props = GLOW_MATERIAL_PROPS("#5b8cff", 0);
    expect(props.emissiveIntensity).toBeCloseTo(glow.baseEmissive);
  });

  it("emissiveIntensity at intensity=1 equals glow.peakEmissive", () => {
    const props = GLOW_MATERIAL_PROPS("#5b8cff", 1);
    expect(props.emissiveIntensity).toBeCloseTo(glow.peakEmissive);
  });

  it("emissiveIntensity scales linearly between base and peak", () => {
    const mid = GLOW_MATERIAL_PROPS("#5b8cff", 0.5).emissiveIntensity;
    const expected = glow.baseEmissive + 0.5 * (glow.peakEmissive - glow.baseEmissive);
    expect(mid).toBeCloseTo(expected);
  });
});

// ── clusterGlow helper ─────────────────────────────────────────────────────
describe("clusterGlow", () => {
  it("returns a semi-transparent hex string", () => {
    const g = clusterGlow("core");
    expect(g).toMatch(/^#[0-9a-f]{8}$/i);
  });

  it("returns fallback for unknown cluster", () => {
    // Cast to test the fallback branch
    const g = clusterGlow("unknown" as Cluster);
    expect(g).toBeDefined();
    expect(typeof g).toBe("string");
  });
});
