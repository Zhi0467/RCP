import { useEffect, useRef, useState } from "react";
import { createProjectRepositoryRequest, type ProjectRepositoryRequest } from "../core/api";
import type { ProjectSnapshot } from "../core/types";
import { projectProvisioningHash } from "./projectSetupModel";

/** Add a repository, or connect the one the Repos list chose, through a setup request. */
export function RepositoryRequests({
  project,
  disabled,
  connectAlias,
  onCloseConnect,
}: {
  project: Pick<ProjectSnapshot, "id" | "machines">;
  disabled: boolean;
  connectAlias: string | null;
  onCloseConnect: () => void;
}) {
  const [adding, setAdding] = useState(false);
  const editing: { kind: "add_repository" | "connect_repository"; alias: string } | null =
    connectAlias !== null
      ? { kind: "connect_repository", alias: connectAlias }
      : adding
        ? { kind: "add_repository", alias: "" }
        : null;
  const [alias, setAlias] = useState("");
  const [source, setSource] = useState("");
  const [machine, setMachine] = useState(project.machines[0]?.alias ?? "");
  const [truth, setTruth] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const active = useRef(true);
  useEffect(() => {
    active.current = true;
    return () => {
      active.current = false;
    };
  }, []);
  useEffect(() => {
    setSource("");
    setError(null);
  }, [connectAlias]);

  function close() {
    setAdding(false);
    onCloseConnect();
    setAlias("");
    setSource("");
    setTruth(true);
    setError(null);
  }

  async function submit() {
    if (!editing || disabled || busy) return;
    const body: ProjectRepositoryRequest =
      editing.kind === "connect_repository"
        ? { kind: editing.kind, alias: editing.alias, source: source.trim() }
        : {
            kind: editing.kind,
            repository: {
              alias: alias.trim(),
              source: source.trim() || null,
              machine_alias: machine,
              count_as_project_truth: truth,
            },
          };
    setBusy(true);
    setError(null);
    try {
      const request = await createProjectRepositoryRequest(project.id, body);
      if (active.current) window.location.hash = projectProvisioningHash(request.request_id);
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (active.current) setBusy(false);
    }
  }

  return (
    <div className="repository-requests">
      {!editing ? (
        <button
          type="button"
          className="button secondary"
          disabled={disabled}
          onClick={() => setAdding(true)}
        >
          Add repository
        </button>
      ) : (
        <form
          onSubmit={(event) => {
            event.preventDefault();
            void submit();
          }}
        >
          <fieldset disabled={disabled || busy}>
            <legend>
              {editing.kind === "add_repository"
                ? "Add repository"
                : `Connect ${editing.alias} to GitHub`}
            </legend>
            {editing.kind === "add_repository" && (
              <label className="setup-field">
                Alias
                <input
                  name="alias"
                  required
                  pattern="[a-z][a-z0-9\-]{0,47}"
                  value={alias}
                  onChange={(event) => setAlias(event.target.value)}
                />
              </label>
            )}
            <label className="setup-field">
              GitHub URL{editing.kind === "add_repository" ? " (optional)" : ""}
              <input
                name="source"
                required={editing.kind === "connect_repository"}
                value={source}
                onChange={(event) => setSource(event.target.value)}
                placeholder="https://github.com/lab/research.git"
              />
            </label>
            {editing.kind === "add_repository" && (
              <>
                {!source.trim() && <p>Server only · Its code is not backed up.</p>}
                <label className="setup-field">
                  Machine
                  <select
                    name="machine"
                    required
                    value={machine}
                    onChange={(event) => setMachine(event.target.value)}
                  >
                    {project.machines.map((item) => (
                      <option key={item.alias} value={item.alias}>
                        {item.alias}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="check-control">
                  <input
                    name="truth"
                    type="checkbox"
                    checked={truth}
                    onChange={(event) => setTruth(event.target.checked)}
                  />
                  Count as project truth (agents may write here)
                </label>
              </>
            )}
            <div className="setup-actions">
              <button type="button" className="button secondary" onClick={close}>
                Cancel
              </button>
              <button
                type="submit"
                className="button primary"
                disabled={!source.trim() && editing.kind === "connect_repository"}
              >
                Create setup request
              </button>
            </div>
          </fieldset>
        </form>
      )}
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
