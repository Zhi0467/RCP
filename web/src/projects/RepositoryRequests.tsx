import { useEffect, useRef, useState } from "react";
import { api, createProjectRepositoryRequest, type ProjectRepositoryRequest } from "../core/api";
import type { ProjectSnapshot } from "../core/types";
import { PathPicker } from "./PathPicker";
import {
  defaultRepositoryName,
  githubRepositoryName,
  projectProvisioningHash,
} from "./projectSetupModel";

type AddMode = "folder" | "github";

const ADD_MODES: { id: AddMode; label: string }[] = [
  { id: "folder", label: "Folder" },
  { id: "github", label: "GitHub" },
];

function folderName(path: string): string {
  return path.replace(/\/+$/, "").split("/").pop() ?? "";
}

/**
 * Add a repository on the base machine, or connect the one the Repos list chose.
 * A team project adds through a setup request; a personal one adds a folder
 * directly, since it has nothing to clone with.
 */
export function RepositoryRequests({
  project,
  spaceKind,
  baseMachineId,
  disabled,
  connectAlias,
  onCloseConnect,
  onAdded,
}: {
  project: Pick<ProjectSnapshot, "id" | "name" | "repositories">;
  spaceKind: "personal" | "team";
  /** The base machine's space machine id, which the folder picker browses. */
  baseMachineId: string | null;
  disabled: boolean;
  connectAlias: string | null;
  onCloseConnect: () => void;
  onAdded: (project: ProjectSnapshot) => void;
}) {
  const [adding, setAdding] = useState(false);
  const [mode, setMode] = useState<AddMode>("folder");
  const [alias, setAlias] = useState("");
  // A typed name stays; otherwise the name follows the folder or GitHub URL.
  const [nameEdited, setNameEdited] = useState(false);
  const [source, setSource] = useState("");
  const [path, setPath] = useState("");
  const [browsing, setBrowsing] = useState(false);
  const [truth, setTruth] = useState(false);
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

  const defaultName = (candidate: string) =>
    defaultRepositoryName(
      [candidate, project.name],
      project.repositories.map((repository) => repository.alias),
    );

  function close() {
    setAdding(false);
    onCloseConnect();
    setMode("folder");
    setAlias("");
    setNameEdited(false);
    setSource("");
    setPath("");
    setBrowsing(false);
    setTruth(false);
    setError(null);
  }

  async function send(body: ProjectRepositoryRequest) {
    if (disabled || busy) return;
    setBusy(true);
    setError(null);
    try {
      if (spaceKind === "personal" && body.kind === "add_repository") {
        const saved = await api<ProjectSnapshot>(
          `/api/projects/${encodeURIComponent(project.id)}/repositories`,
          {
            method: "POST",
            body: JSON.stringify({
              alias: body.repository.alias,
              path: body.repository.path,
              count_as_project_truth: body.repository.count_as_project_truth,
            }),
          },
        );
        if (!active.current) return;
        onAdded(saved);
        close();
        return;
      }
      const request = await createProjectRepositoryRequest(project.id, body);
      if (active.current) window.location.hash = projectProvisioningHash(request.request_id);
    } catch (caught) {
      if (active.current) setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (active.current) setBusy(false);
    }
  }

  const ready = alias.trim() && (mode === "folder" ? path : source.trim());
  const errorLine = error && <p role="alert">{error}</p>;

  if (connectAlias !== null) {
    return (
      <form
        className="repository-requests"
        onSubmit={(event) => {
          event.preventDefault();
          void send({ kind: "connect_repository", alias: connectAlias, source: source.trim() });
        }}
      >
        <fieldset disabled={disabled || busy}>
          <legend>Connect {connectAlias} to GitHub</legend>
          <input
            name="source"
            aria-label="GitHub URL"
            required
            value={source}
            onChange={(event) => setSource(event.target.value)}
            placeholder="https://github.com/lab/research.git"
          />
          <div className="setup-actions">
            <button type="button" className="button secondary" onClick={close}>
              Cancel
            </button>
            <button type="submit" className="button primary" disabled={!source.trim()}>
              Connect
            </button>
          </div>
        </fieldset>
        {errorLine}
      </form>
    );
  }

  if (!adding) {
    return (
      <div className="repository-requests">
        <button
          type="button"
          className="button secondary"
          disabled={disabled}
          onClick={() => setAdding(true)}
        >
          Add repository
        </button>
      </div>
    );
  }

  return (
    <form
      className="repository-requests"
      onSubmit={(event) => {
        event.preventDefault();
        if (!ready) return;
        void send({
          kind: "add_repository",
          repository: {
            alias: alias.trim(),
            ...(mode === "folder" ? { path } : { source: source.trim() }),
            count_as_project_truth: truth,
          },
        });
      }}
    >
      <fieldset disabled={disabled || busy} aria-label="Add repo">
        <header className="repository-add-header">
          <strong>Add repo</strong>
          {spaceKind === "team" && (
            <div className="repository-add-modes" role="group" aria-label="Repository source">
              {ADD_MODES.map((item) => (
                <button
                  type="button"
                  key={item.id}
                  aria-pressed={mode === item.id}
                  onClick={() => {
                    setMode(item.id);
                    setBrowsing(false);
                    setError(null);
                    if (!nameEdited) {
                      setAlias(
                        item.id === "folder"
                          ? path && defaultName(folderName(path))
                          : source.trim() && defaultName(githubRepositoryName(source)),
                      );
                    }
                  }}
                >
                  {item.label}
                </button>
              ))}
            </div>
          )}
        </header>
        {mode === "folder" ? (
          <div className="repository-add-path">
            <input
              name="path"
              aria-label="Folder"
              placeholder="No folder chosen"
              readOnly
              value={path}
            />
            <button
              type="button"
              className="button secondary"
              disabled={baseMachineId === null}
              onClick={() => setBrowsing((current) => !current)}
            >
              Choose
            </button>
          </div>
        ) : (
          <input
            name="source"
            aria-label="GitHub URL"
            required
            value={source}
            onChange={(event) => {
              setSource(event.target.value);
              if (!nameEdited) {
                const name = githubRepositoryName(event.target.value);
                setAlias(name && defaultName(name));
              }
            }}
            placeholder="https://github.com/lab/research.git"
          />
        )}
        {mode === "folder" && browsing && baseMachineId !== null && (
          <PathPicker
            machineId={baseMachineId}
            initialPath={path || null}
            pickLabel="Use this folder"
            onClose={() => setBrowsing(false)}
            onPick={(picked) => {
              setPath(picked);
              if (!nameEdited) setAlias(defaultName(folderName(picked)));
              setBrowsing(false);
            }}
          />
        )}
        <input
          name="alias"
          aria-label="Name"
          placeholder="Name"
          required
          pattern="[a-z][a-z0-9\-]{0,47}"
          value={alias}
          onChange={(event) => {
            setAlias(event.target.value);
            setNameEdited(true);
          }}
        />
        <label className="check-control">
          <input
            name="truth"
            type="checkbox"
            checked={truth}
            onChange={(event) => setTruth(event.target.checked)}
          />
          Agents can write
        </label>
        <div className="setup-actions">
          <button type="button" className="button secondary" onClick={close}>
            Cancel
          </button>
          <button type="submit" className="button primary" disabled={!ready}>
            Add
          </button>
        </div>
      </fieldset>
      {errorLine}
    </form>
  );
}
