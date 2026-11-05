import { useState } from "react";
import { addProjectMachine } from "../core/api";
import { errorMessage } from "../core/errors";
import { machineHostLabel, machinesToAdd } from "./spaceMachines";
import type { ProjectSnapshot, SpaceMachine } from "../core/types";
import { NewMachineForm } from "./MachineCard";
import { AddMachineTile, MachineTile } from "./MachineTile";

/** Appends a space machine to this project's manifest, picking a card or creating one. */
export function AddProjectMachine({
  projectId,
  spaceKind,
  machines,
  writesDisabled,
  onCreated,
  onAdded,
  onClose,
}: {
  projectId: string;
  spaceKind: "personal" | "team";
  machines: SpaceMachine[];
  writesDisabled: boolean;
  onCreated: (machine: SpaceMachine) => void;
  onAdded: (project: ProjectSnapshot, machineId: string) => void;
  onClose: () => void;
}) {
  const [creating, setCreating] = useState(false);
  const [adding, setAdding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const candidates = machinesToAdd(machines, projectId);

  const add = async (machine: SpaceMachine) => {
    if (adding) return;
    setAdding(machine.machine_id);
    setError(null);
    try {
      onAdded(await addProjectMachine(projectId, machine.machine_id), machine.machine_id);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setAdding(null);
    }
  };

  return (
    <div className="add-project-machine open" data-add-project-machine="">
      <header className="add-project-machine-header">
        <strong>Add a machine to this project</strong>
        <button className="button secondary compact" type="button" onClick={onClose}>
          Cancel
        </button>
      </header>
      {creating ? (
        <NewMachineForm
          writesDisabled={writesDisabled}
          accountRequired={spaceKind === "team"}
          onCancel={() => setCreating(false)}
          onCreated={(machine) => {
            onCreated(machine);
            setCreating(false);
            void add(machine);
          }}
        />
      ) : (
        <div className="machine-tiles" role="group" aria-label="Space machines">
          {candidates.map((machine) => (
            <MachineTile
              key={machine.machine_id}
              machineId={machine.machine_id}
              name={machine.name}
              hostLabel={machineHostLabel(machine.host, spaceKind)}
              account={machine.os_account}
              signals={adding === machine.machine_id ? [{ label: "Adding…", tone: "pending" }] : []}
              disabled={writesDisabled || adding !== null}
              onSelect={() => void add(machine)}
            />
          ))}
          <AddMachineTile
            label="New machine"
            disabled={writesDisabled || adding !== null}
            onSelect={() => setCreating(true)}
          />
        </div>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
