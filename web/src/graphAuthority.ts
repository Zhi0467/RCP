import type { AgentTask, ProjectSnapshot } from "./types";

export function projectGraphMutationsDisabled(
  project: Pick<ProjectSnapshot, "graph_mutation">,
): boolean {
  return !project.graph_mutation.available;
}

export function projectGraphMutationFailureLabel(
  project: Pick<ProjectSnapshot, "graph_mutation">,
): string | null {
  return project.graph_mutation.available ? null : project.graph_mutation.reason;
}

export function taskMayMutateGraph(task: AgentTask): boolean {
  // Only ingestion is gated on graph health. Work's operational result is
  // independent of its optional graph reflection: a degraded graph may reject
  // the patch, but must not strand Resume or Retry for repository work that can
  // still complete safely.
  return task.kind === "seed" || task.kind === "refresh";
}
