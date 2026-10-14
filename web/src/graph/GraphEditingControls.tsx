import type { GraphState } from "../core/types";
import type { GraphEditingProps } from "./canvasEditing";

export type { GraphEditingProps } from "./canvasEditing";

// Temporary compatibility for GraphViews until the canvas slice removes this component.
export function GraphEditingControls(
  _props: GraphEditingProps & {
    graph: GraphState;
    connection?: { source: string; target: string } | null;
  },
) {
  return null;
}
