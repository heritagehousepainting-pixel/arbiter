// ─────────────────────────────────────────────────────────────────────────────
// LANE 5 — Design token system (aesthetic & motion polish)
// OWNED BY LANE 5. Do not edit from other lanes.
//
// Importing:
//   import { theme, clusterGlow, motionDuration, GLOW_MATERIAL_PROPS } from "../theme/theme";
//
// Scene / UI seams:
//   • meshStandardMaterial: spread `...GLOW_MATERIAL_PROPS(color, intensity)` for premium emissive
//   • Any component: use `theme.motion.*` durations + easing for JS/CSS transitions
//   • CLUSTER_ACCENT mirrors CLUSTER_COLOR from contract.ts with richer HSL-tuned values
//   • Use `prefersReducedMotion()` helper before any animation
// ─────────────────────────────────────────────────────────────────────────────

import type { Cluster } from "../contract";

// ── Base palette ──────────────────────────────────────────────────────────────
export const palette = {
  // Core backgrounds — deep space
  bg:          "#05060a",   // canvas void — App.tsx reads theme.bg
  bgSurface:   "#090b13",   // panel/card elevated surface
  bgOverlay:   "#0d1120",   // modal / tooltip overlay
  bgRim:       "#141828",   // border/divider

  // Typography
  text:        "#e7ecff",   // primary — high contrast on dark
  textSub:     "#a8b4cf",   // secondary labels
  muted:       "#8d99ae",   // disabled / placeholder
  textDim:     "#4a5468",   // very dim — decorative only

  // Semantic: state
  ok:          "#06d6a0",   // healthy / bullish — teal
  bad:         "#ef476f",   // alert / bearish — coral-red
  warn:        "#ffd166",   // warning / in-progress — gold
  neutral:     "#8d99ae",   // neutral / stale

  // Accent — primary interactive
  accent:      "#5b8cff",   // blue — links, focus, sources cluster
  accentDim:   "#2a4a99",   // subtle accent bg

  // Glow corona colors (used in materials + bloom)
  glowCore:    "#ef476f",   // decision core — hot pink
  glowFigure:  "#ffd166",   // figures — warm gold
  glowCouncil: "#06d6a0",   // council — mint
  glowIdea:    "#c77dff",   // ideas — violet
  glowTrade:   "#4cc9f0",   // execution/trades — ice blue
  glowLearn:   "#80ed99",   // learning loop — spring green

  // Pure
  white:       "#ffffff",
  black:       "#000000",
} as const;

// ── Elevation system (box-shadow / z-depth) ───────────────────────────────────
export const elevation = {
  /** Flat — cards resting on bg */
  e0: "0 0 0 1px rgba(255,255,255,0.04)",
  /** Subtle lift — tooltips, chips */
  e1: "0 2px 8px rgba(0,0,0,0.55), 0 0 0 1px rgba(255,255,255,0.06)",
  /** Panels, modals */
  e2: "0 8px 32px rgba(0,0,0,0.7), 0 0 0 1px rgba(255,255,255,0.08)",
  /** Floating overlays */
  e3: "0 20px 64px rgba(0,0,0,0.85), 0 0 0 1px rgba(255,255,255,0.10)",
  /** Glow rim — focus / hover highlights */
  glow: (color: string, spread = 8) =>
    `0 0 ${spread}px 2px ${color}40, 0 0 ${spread * 2}px 4px ${color}20`,
} as const;

// ── Glow / depth tokens — used to tune Bloom + materials ─────────────────────
export const glow = {
  /** Intensity fed to meshStandardMaterial.emissiveIntensity at rest */
  baseEmissive:     0.28,
  /** Peak emissive when node intensity = 1 */
  peakEmissive:     2.4,
  /** Bloom luminanceThreshold — anything brighter than this blooms */
  bloomThreshold:   0.15,
  /** Bloom luminance smoothing — gradient width */
  bloomSmoothing:   0.4,
  /** Bloom intensity multiplier */
  bloomIntensity:   1.6,
  /** Bloom mipmap radius (0..1) */
  bloomRadius:      0.72,
  /** Vignette darkness */
  vignetteDarkness: 0.65,
  /** Vignette offset — how far in from edge */
  vignetteOffset:   0.42,
  /** ChromaticAberration pixel offset (as Vector2 components) */
  chromaOffset:     0.0007,
} as const;

// ── Motion / easing tokens ────────────────────────────────────────────────────
export const motion = {
  // Durations (ms)
  instant:    80,
  fast:       160,
  normal:     280,
  slow:       480,
  xslow:      800,
  // Easings (CSS cubic-bezier strings)
  easeOut:    "cubic-bezier(0.16, 1, 0.3, 1)",   // snappy deceleration
  easeIn:     "cubic-bezier(0.4, 0, 1, 1)",
  easeInOut:  "cubic-bezier(0.76, 0, 0.24, 1)",
  spring:     "cubic-bezier(0.34, 1.56, 0.64, 1)", // slight overshoot
  linear:     "linear",
} as const;

// ── Typography scale ──────────────────────────────────────────────────────────
export const type = {
  // Sizes (rem)
  xs:   "0.625rem",   // 10px
  sm:   "0.75rem",    // 12px
  base: "0.875rem",   // 14px
  md:   "1rem",       // 16px
  lg:   "1.125rem",   // 18px
  xl:   "1.375rem",   // 22px
  "2xl":"1.75rem",    // 28px
  "3xl":"2.25rem",    // 36px
  // Weights
  normal: 400,
  medium: 500,
  semi:   600,
  bold:   700,
  // Line heights
  tight:  1.2,
  snug:   1.4,
  normal_lh: 1.6,
  // Fonts
  mono: "'Fira Code', 'JetBrains Mono', 'Cascadia Code', ui-monospace, monospace",
  sans: "'Inter', 'DM Sans', system-ui, -apple-system, sans-serif",
} as const;

// ── Cluster accent — enriched from contract.ts CLUSTER_COLOR ─────────────────
// These are the same hues but you can opt into richer HSL-shifted versions.
// Scene/UI: import CLUSTER_ACCENT instead of CLUSTER_COLOR for richer tokens.
export const CLUSTER_ACCENT: Record<Cluster, {
  base: string;
  glow: string;
  dim: string;
  emissive: number;
}> = {
  sources:   { base: "#5b8cff", glow: "#5b8cff80", dim: "#1a2a5c", emissive: 0.4 },
  figures:   { base: "#ffd166", glow: "#ffd16680", dim: "#4a3a10", emissive: 0.5 },
  council:   { base: "#06d6a0", glow: "#06d6a080", dim: "#063d2d", emissive: 0.45 },
  core:      { base: "#ef476f", glow: "#ef476f90", dim: "#4a0d1d", emissive: 0.7 },
  ideas:     { base: "#c77dff", glow: "#c77dff80", dim: "#3a1566", emissive: 0.45 },
  execution: { base: "#4cc9f0", glow: "#4cc9f080", dim: "#0d3042", emissive: 0.4 },
  market:    { base: "#e7ecff", glow: "#e7ecff60", dim: "#2a2d3a", emissive: 0.3 },
  learning:  { base: "#80ed99", glow: "#80ed9980", dim: "#0d3020", emissive: 0.45 },
  infra:     { base: "#8d99ae", glow: "#8d99ae60", dim: "#1a1e28", emissive: 0.25 },
};

// ── Glow material helper — scene/ui can spread this onto meshStandardMaterial ─
// Usage in SceneRoot or any mesh:
//   <meshStandardMaterial {...GLOW_MATERIAL_PROPS(color, intensity)} />
export function GLOW_MATERIAL_PROPS(
  color: string,
  intensity: number = 0,  // 0..1 from NodeState.intensity
): {
  color: string;
  emissive: string;
  emissiveIntensity: number;
  roughness: number;
  metalness: number;
} {
  const emissive = glow.baseEmissive + intensity * (glow.peakEmissive - glow.baseEmissive);
  return {
    color,
    emissive: color,
    emissiveIntensity: emissive,
    roughness: 0.15,
    metalness: 0.5,
  };
}

// ── Reduced-motion helper ─────────────────────────────────────────────────────
// Returns true when the OS/browser requests reduced motion.
// Gate all animations behind this. Works in all browsers + SSR-safe.
export function prefersReducedMotion(): boolean {
  if (typeof window === "undefined") return false;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

// ── clusterGlow helper — convenience wrapper ──────────────────────────────────
export function clusterGlow(cluster: Cluster): string {
  return CLUSTER_ACCENT[cluster]?.glow ?? "#ffffff40";
}

// ── Root theme export — App.tsx reads theme.bg ────────────────────────────────
// Keep this the canonical surface for top-level color access.
export const theme = {
  // Flat access (App.tsx reads these)
  bg:      palette.bg,
  text:    palette.text,
  muted:   palette.muted,
  ok:      palette.ok,
  bad:     palette.bad,
  warn:    palette.warn,
  accent:  palette.accent,
  // Sub-objects for richer access
  palette,
  elevation,
  glow,
  motion,
  type,
  cluster: CLUSTER_ACCENT,
} as const;

export default theme;
