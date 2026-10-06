import type { Machine, SpaceMachine, SpaceMachineUpdateRequest } from "../core/types";

export type MachineSettingsRecord = SpaceMachine;

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

/** The backend's `AddProjectMachineRequest.alias` limit. */
export const MACHINE_ALIAS_MAX_LENGTH = 48;

/** A manifest alias suggested from a machine's display name. */
export function suggestedMachineAlias(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .slice(0, MACHINE_ALIAS_MAX_LENGTH)
    .replace(/^-+|-+$/g, "");
}

/** A machine's name in one project: its suggested alias, numbered past any already taken. */
export function projectMachineAlias(name: string, taken: readonly string[]): string {
  const base = suggestedMachineAlias(name) || "machine";
  const used = new Set(taken);
  if (!used.has(base)) return base;
  for (let index = 2; ; index += 1) {
    const suffix = `-${index}`;
    const candidate = `${base.slice(0, MACHINE_ALIAS_MAX_LENGTH - suffix.length)}${suffix}`;
    if (!used.has(candidate)) return candidate;
  }
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

/**
 * Runs writable-path edits one at a time, each built from the latest saved record.
 *
 * Every PATCH carries the whole list, so two overlapping edits would each send a
 * list missing the other's change. A second edit while one is pending is refused.
 */
export function createPathEditor(
  save: (machineId: string, request: SpaceMachineUpdateRequest) => Promise<SpaceMachine>,
  field: "writable_paths" | "hidden_folders" = "writable_paths",
) {
  let saved: SpaceMachine | null = null;
  let pending = false;
  return {
    get pending() {
      return pending;
    },
    /** Adopt a record from outside; ignored while an edit's own answer is outstanding. */
    sync(record: SpaceMachine | null) {
      if (!pending) saved = record;
    },
    async edit(change: WritablePathEdit): Promise<SpaceMachine> {
      if (pending) throw new Error("Another path change is still saving.");
      if (!saved) throw new Error("This machine has no space record yet.");
      pending = true;
      try {
        const { writable_paths: paths } = writablePathsRequest(
          { writable_paths: saved[field] },
          change,
        );
        saved = await save(saved.machine_id, { [field]: paths });
        return saved;
      } finally {
        pending = false;
      }
    },
  };
}

/**
 * The card an SSH repository is on. The chosen id wins while its host still
 * matches; otherwise a host names a card only when exactly one card has it,
 * since two accounts on one host are two cards.
 */
export function setupMachineSelection(
  machines: readonly SpaceMachine[],
  selectedId: string | null,
  host: string,
): SpaceMachine | null {
  const chosen = machines.find((machine) => machine.machine_id === selectedId);
  if (chosen && chosen.host === host) return chosen;
  const matches = machines.filter((machine) => machine.host === host);
  return matches.length === 1 ? matches[0] : null;
}

export type MachineSave = (
  machineId: string,
  request: SpaceMachineUpdateRequest,
) => Promise<SpaceMachine>;

/**
 * Run one machine card's saves one at a time. Each answer is a whole record, so a
 * slower earlier answer would otherwise replace a newer one.
 */
export function createSerialMachineSave(save: MachineSave): MachineSave {
  let last: Promise<unknown> = Promise.resolve();
  return (machineId, request) => {
    const next = last.catch(() => undefined).then(() => save(machineId, request));
    last = next;
    return next;
  };
}
