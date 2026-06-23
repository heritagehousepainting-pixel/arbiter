// LANE 5 — Post-processing pass. OWNED BY LANE 5.
//
// Mounts inside App.tsx's <Canvas> as <PostFX />.
// Also loads global.css as a side-effect — the only way to inject global styles
// without editing any frozen file.
//
// Effect stack (premium / tasteful):
//   1. Bloom  — makes emissive nodes/core glow (luminance-gated, mipmap-quality)
//   2. Vignette — dark edges focus the eye on the constellation center
//   3. ChromaticAberration — very subtle prismatic fringe (premium depth cue)
//
// All heavy effects are disabled when prefers-reduced-motion: reduce is set.
// SMAA (alias smoothing) runs even in reduced-motion mode — it's perceptual, not
// motion. Bloom perf note: mipmap bloom is GPU-cheap (~0.2ms on discrete GPU);
// disable `mipmapBlur` or lower `levels` if profiling shows pressure.

import "../styles/global.css"; // side-effect: global typography + base styles

import { BlendFunction } from "postprocessing";
import {
  Bloom,
  ChromaticAberration,
  EffectComposer,
  SMAA,
  Vignette,
} from "@react-three/postprocessing";
import * as THREE from "three";
import { useMemo } from "react";
import { glow, prefersReducedMotion } from "../theme/theme";

// ── Minimal pass: SMAA only (reduced-motion / low-power) ─────────────────────
function MinimalFX() {
  return (
    <EffectComposer multisampling={0}>
      <SMAA />
    </EffectComposer>
  );
}

// ── Premium pass: Bloom + Vignette + ChromaticAberration + SMAA ──────────────
function PremiumFX() {
  // Chromatic aberration offset — very subtle, just a depth cue
  const chromaVec = useMemo(
    () => new THREE.Vector2(glow.chromaOffset, glow.chromaOffset),
    [],
  );

  return (
    <EffectComposer multisampling={0}>
      {/* Anti-aliasing — always first to stabilize the input image */}
      <SMAA />

      {/*
       * Bloom — the main premium glow.
       *   luminanceThreshold: only pixels brighter than this value bloom.
       *     Our emissiveIntensity range (0.28 → 2.4) means bright nodes bloom
       *     strongly; dim ambient geometry stays clean.
       *   luminanceSmoothing: gradient width so the glow edge is soft.
       *   mipmapBlur: quality mipmap approach — cheaper + nicer than kawase.
       *   intensity: overall multiplier. 1.6 is lush but not blown out.
       *   radius: 0..1 bloom spread. 0.72 = wide corona.
       */}
      <Bloom
        luminanceThreshold={glow.bloomThreshold}
        luminanceSmoothing={glow.bloomSmoothing}
        mipmapBlur
        intensity={glow.bloomIntensity}
        radius={glow.bloomRadius}
        blendFunction={BlendFunction.ADD}
      />

      {/*
       * Vignette — darkens screen corners to focus on the constellation.
       *   eskil: false = classic photographic vignette (not Eskil's technique).
       *   offset: how far in from edge (0 = full screen, 1 = edge only).
       *   darkness: 0..1 how dark the corners get.
       */}
      <Vignette
        eskil={false}
        offset={glow.vignetteOffset}
        darkness={glow.vignetteDarkness}
        blendFunction={BlendFunction.NORMAL}
      />

      {/*
       * ChromaticAberration — prismatic RGB split at very low magnitude.
       *   Gives a subtle "optical glass" feel; imperceptible but adds depth.
       *   radialModulation: false = uniform offset (not radial distortion).
       */}
      <ChromaticAberration
        offset={chromaVec}
        radialModulation={false}
        modulationOffset={0}
        blendFunction={BlendFunction.NORMAL}
      />
    </EffectComposer>
  );
}

// ── Public export — mounted by App.tsx inside the <Canvas> ───────────────────
export function PostFX() {
  const reduced = prefersReducedMotion();
  return reduced ? <MinimalFX /> : <PremiumFX />;
}
