// Type shim for d3-force-3d (no official @types package).
// Provides minimal typings needed by layout.ts.
declare module "d3-force-3d" {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type NodeDatum = { x?: number; y?: number; z?: number; [key: string]: any };
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  type LinkDatum = { source: any; target: any; [key: string]: any };

  interface Force<N extends NodeDatum> {
    (alpha: number): void;
    initialize?(nodes: N[], random?: () => number): void;
  }

  interface Simulation<N extends NodeDatum> {
    force(name: string, force?: Force<N> | null): this;
    nodes(): N[];
    nodes(nodes: N[]): this;
    alpha(): number;
    alpha(alpha: number): this;
    alphaMin(min: number): this;
    alphaDecay(decay: number): this;
    tick(iterations?: number): this;
    stop(): this;
    restart(): this;
  }

  interface ManyBodyForce<N extends NodeDatum> extends Force<N> {
    strength(): number;
    strength(strength: number | ((d: N, i: number, nodes: N[]) => number)): this;
    distanceMin(): number;
    distanceMin(min: number): this;
    distanceMax(): number;
    distanceMax(max: number): this;
  }

  interface CollideForce<N extends NodeDatum> extends Force<N> {
    radius(): number | ((d: N, i: number, nodes: N[]) => number);
    radius(radius: number | ((d: N, i: number, nodes: N[]) => number)): this;
    strength(strength: number): this;
    iterations(iterations: number): this;
  }

  interface LinkForce<N extends NodeDatum, L extends LinkDatum> extends Force<N> {
    links(): L[];
    links(links: L[]): this;
    id(id: (d: N, i: number, nodes: N[]) => string): this;
    distance(): number | ((d: L, i: number, links: L[]) => number);
    distance(distance: number | ((d: L, i: number, links: L[]) => number)): this;
    strength(): number | ((d: L, i: number, links: L[]) => number);
    strength(strength: number | ((d: L, i: number, links: L[]) => number)): this;
    iterations(iterations: number): this;
  }

  interface CenterForce<N extends NodeDatum> extends Force<N> {
    x(x: number): this;
    y(y: number): this;
    z(z: number): this;
  }

  interface RadialForce<N extends NodeDatum> extends Force<N> {
    radius(r: number | ((d: N) => number)): this;
    x(x: number): this;
    y(y: number): this;
    z(z: number): this;
    strength(s: number): this;
  }

  interface AxisForce<N extends NodeDatum> extends Force<N> {
    x?: (x: number | ((d: N) => number)) => this;
    y?: (y: number | ((d: N) => number)) => this;
    strength(s: number | ((d: N) => number)): this;
  }

  interface ForceX<N extends NodeDatum> extends AxisForce<N> {
    x(x: number | ((d: N) => number)): this;
  }
  interface ForceY<N extends NodeDatum> extends AxisForce<N> {
    y(y: number | ((d: N) => number)): this;
  }
  interface ForceZ<N extends NodeDatum> extends AxisForce<N> {
    z(z: number | ((d: N) => number)): this;
  }

  export function forceSimulation<N extends NodeDatum>(
    nodes?: N[],
    numDimensions?: number,
  ): Simulation<N>;

  export function forceManyBody<N extends NodeDatum>(): ManyBodyForce<N>;
  export function forceCenter<N extends NodeDatum>(
    x?: number, y?: number, z?: number,
  ): CenterForce<N>;
  export function forceCollide<N extends NodeDatum>(
    radius?: number | ((d: N) => number),
  ): CollideForce<N>;
  export function forceLink<N extends NodeDatum, L extends LinkDatum>(
    links?: L[],
  ): LinkForce<N, L>;
  export function forceRadial<N extends NodeDatum>(
    radius: number | ((d: N) => number),
    x?: number, y?: number, z?: number,
  ): RadialForce<N>;
  export function forceX<N extends NodeDatum>(
    x?: number | ((d: N) => number),
  ): ForceX<N>;
  export function forceY<N extends NodeDatum>(
    y?: number | ((d: N) => number),
  ): ForceY<N>;
  export function forceZ<N extends NodeDatum>(
    z?: number | ((d: N) => number),
  ): ForceZ<N>;
}
