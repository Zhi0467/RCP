import type { ProjectSnapshot } from "../core/types";

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
