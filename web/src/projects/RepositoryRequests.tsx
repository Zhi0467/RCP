import { useEffect, useRef, useState } from "react";
import { createProjectRepositoryRequest, type ProjectRepositoryRequest } from "../core/api";
import type { ProjectSnapshot, Repository } from "../core/types";
import { projectProvisioningHash } from "./projectSetupModel";

// Settings provenance is supplied by the effective repository inventory (W4).
type SettingsRepository = Repository & {
  source?: "github" | "server_only";
  github_identity?: string | null;
  can_connect?: boolean;
};

export function RepositoryRequests({
  project,
  disabled,
}: {
  project: Pick<ProjectSnapshot, "id" | "repositories" | "machines">;
  disabled: boolean;
}) {
  const [editing, setEditing] = useState<{
    kind: "add_repository" | "connect_repository";
    alias: string;
  } | null>(null);
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

  function open(kind: "add_repository" | "connect_repository", repositoryAlias = "") {
    setEditing({ kind, alias: repositoryAlias });
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
      {(project.repositories as SettingsRepository[]).map((repository) => (
        <div className="repository-request-row" key={repository.alias}>
          <span>
            {repository.alias} ·{" "}
            {repository.source === "server_only" ? "Server only" : repository.github_identity}
          </span>
          {repository.source === "server_only" && <span>Its code is not backed up.</span>}
          {repository.source === "server_only" && repository.can_connect && (
            <button
              type="button"
              className="button secondary"
              disabled={disabled || busy}
              onClick={() => open("connect_repository", repository.alias)}
            >
              Connect to GitHub
            </button>
          )}
        </div>
      ))}
      {!editing ? (
        <button
          type="button"
          className="button secondary"
          disabled={disabled}
          onClick={() => open("add_repository")}
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
                  pattern="[a-z][a-z0-9-]{0,47}"
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
              <button type="button" className="button secondary" onClick={() => setEditing(null)}>
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
