import type { GraphBranchChanges, GraphState } from "./types";

/** A display lens only: callers retain the complete live graph for edits and pickers. */
export function branchGraphProjection(
  graph: GraphState,
  changes: GraphBranchChanges,
  context: ReadonlySet<string> | null,
): GraphState {
  const nodes = { ...graph.nodes };
  const edges = { ...graph.edges };
  for (const change of changes.nodes) {
    if (change.change === "removed" && change.before) nodes[change.node_id] = change.before;
  }
  for (const change of changes.edges) {
    if (change.change === "removed" && change.before) edges[change.edge_id] = change.before;
  }
  const visibleIds = context
    ? new Set([
        ...changes.changed_node_ids,
        ...changes.context_node_ids,
        ...context,
        ...Object.values(graph.nodes)
          .filter((node) => node.draft_touched)
          .map((node) => node.id),
      ])
    : new Set(Object.keys(nodes));
  return {
    ...graph,
    nodes: Object.fromEntries(Object.entries(nodes).filter(([id]) => visibleIds.has(id))),
    edges: Object.fromEntries(
      Object.entries(edges).filter(
        ([, edge]) => visibleIds.has(edge.source) && visibleIds.has(edge.target),
      ),
    ),
  };
}

export function expandBranchContext(
  graph: GraphState,
  visibleIds: ReadonlySet<string>,
): Set<string> {
  const next = new Set(visibleIds);
  for (const edge of Object.values(graph.edges)) {
    if (visibleIds.has(edge.source) || visibleIds.has(edge.target)) {
      next.add(edge.source);
      next.add(edge.target);
    }
  }
  return next;
}
