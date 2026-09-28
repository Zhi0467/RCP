import type { Machine, SpaceMachine, SpaceMachineUpdateRequest } from "./types";

export type WritablePathEdit = { kind: "add"; path: string } | { kind: "remove"; path: string };

/** The PATCH body for one writable-path edit: the whole list, in order, without duplicates. */
export function writablePathsRequest(
  machine: Pick<SpaceMachine, "writable_paths">,
  edit: WritablePathEdit,
): SpaceMachineUpdateRequest {
  const current = machine.writable_paths;
  if (edit.kind === "remove") {
    return { writable_paths: current.filter((path) => path !== edit.path) };
  }
  return {
    writable_paths: current.includes(edit.path) ? [...current] : [...current, edit.path],
  };
}

/** The space record behind one of a project's manifest machines. */
export function spaceMachineForProject(
  machines: readonly SpaceMachine[],
  projectId: string,
  machine: Pick<Machine, "alias">,
): SpaceMachine | null {
  return (
    machines.find((candidate) =>
      candidate.projects.some(
        (project) => project.project_id === projectId && project.alias === machine.alias,
      ),
    ) ?? null
  );
}

/** Space machines a project does not use yet, which "Add machine" can offer. */
export function machinesToAdd(
  machines: readonly SpaceMachine[],
  projectId: string,
): SpaceMachine[] {
  return machines.filter(
    (machine) => !machine.projects.some((project) => project.project_id === projectId),
  );
}

/** An empty host is the machine RCP runs on: the team server in a team space. */
export function machineHostLabel(host: string, spaceKind: "personal" | "team"): string {
  return host || (spaceKind === "team" ? "Team server" : "This machine");
}

/** A manifest alias suggested from a machine's display name. */
export function suggestedMachineAlias(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
}

export function replaceSpaceMachine(
  machines: readonly SpaceMachine[],
  updated: SpaceMachine,
): SpaceMachine[] {
  const found = machines.some((machine) => machine.machine_id === updated.machine_id);
  return found
    ? machines.map((machine) => (machine.machine_id === updated.machine_id ? updated : machine))
    : [...machines, updated];
}
