// Foundation shell (stable): owns data-fetching + composes the lane modules.
//   - scene/SceneRoot  (Lane 3)
//   - ui/CockpitUI      (Lane 4)
//   - theme/theme       (Lane 5)
// Lanes edit their OWN modules, not this file, so they never collide.
import { Canvas } from "@react-three/fiber";
import { type CSSProperties, useEffect, useState } from "react";
import { fetchGraph, fetchState } from "./api";
import type { Graph, State } from "./contract";
import { PostFX } from "./effects/PostFX";
import { SceneRoot } from "./scene/SceneRoot";
import { theme } from "./theme/theme";
import { CockpitUI } from "./ui/CockpitUI";

export function App() {
  const [graph, setGraph] = useState<Graph | null>(null);
  const [state, setState] = useState<State | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    fetchGraph().then(setGraph).catch((e) => setErr(String(e)));
  }, []);

  useEffect(() => {
    let alive = true;
    const tick = () => fetchState().then((s) => alive && setState(s)).catch(() => {});
    tick();
    const id = setInterval(tick, 4000);
    return () => { alive = false; clearInterval(id); };
  }, []);

  // Pin the whole cockpit to the viewport (position:fixed inset:0) so the Canvas
  // always fills the window and resizes with it, and the absolute-positioned
  // overlays anchor to this full-screen container.
  const shell: CSSProperties = {
    position: "fixed",
    inset: 0,
    width: "100vw",
    height: "100vh",
    overflow: "hidden",
    background: theme.bg,
  };

  if (err)
    return (
      <div style={{ ...shell, padding: 24, color: theme.text }}>
        API error: {err}. Is the sidecar on :8910?
      </div>
    );
  if (!graph)
    return <div style={{ ...shell, padding: 24, color: theme.muted }}>Loading constellation…</div>;

  return (
    <div style={shell}>
      <Canvas
        style={{ width: "100%", height: "100%", display: "block" }}
        camera={{ position: [-4, 10, 74], fov: 50 }}
      >
        <color attach="background" args={[theme.bg]} />
        <SceneRoot graph={graph} state={state} onSelect={setSelectedId} />
        <PostFX />
      </Canvas>
      <CockpitUI graph={graph} state={state} selectedId={selectedId} onClose={() => setSelectedId(null)} />
    </div>
  );
}
