import { FolderPlus, LoaderCircle, Trash2, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { createSpaceMachine, updateSpaceMachine } from "../api";
import { errorMessage } from "../errors";
import { writablePathsRequest, type WritablePathEdit } from "../spaceMachines";
import type { SpaceMachine, SpaceMachineCreateRequest } from "../types";
import { PathPicker } from "./PathPicker";

interface Props {
  title: string;
  /** Where the account lives, as a person names it. */
  hostLabel: string;
  osAccount: string;
  /** The space record whose writable paths this card edits; null when the space has none yet. */
  record: SpaceMachine | null;
  level: "space" | "project";
  writesDisabled?: boolean;
  onRecordChange: (machine: SpaceMachine) => void;
  /** Offered only for a space machine no project uses. */
  onDelete?: () => Promise<void>;
  /** Project-only rows, such as provider paths and compute. */
  children?: ReactNode;
}

/** One machine account, shown the same way on the space and the project Settings pages. */
export function MachineCard({
  title,
  hostLabel,
  osAccount,
  record,
  level,
  writesDisabled = false,
  onRecordChange,
  onDelete,
  children,
}: Props) {
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  return (
    <article
      className="provider-machine machine-card"
      data-machine-card={record?.machine_id ?? title}
    >
      <header>
        <strong>{title}</strong>
        <span>
          {hostLabel}
          {osAccount ? ` · ${osAccount}` : ""}
        </span>
        {onDelete && (
          <button
            className="icon-button"
            type="button"
            data-machine-action="delete"
            aria-label={`Remove ${title}`}
            disabled={writesDisabled || deleting}
            onClick={() =>
              void (async () => {
                setDeleting(true);
                setError(null);
                try {
                  await onDelete();
                } catch (failure) {
                  setError(errorMessage(failure));
                  setDeleting(false);
                }
              })()
            }
          >
            {deleting ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
          </button>
        )}
      </header>
      {error && (
        <p className="machine-card-error" role="alert">
          {error}
        </p>
      )}
      {children}
      <WritablePaths
        record={record}
        level={level}
        writesDisabled={writesDisabled}
        onRecordChange={onRecordChange}
      />
    </article>
  );
}

export function WritablePaths({
  record,
  level,
  writesDisabled,
  onRecordChange,
}: {
  record: SpaceMachine | null;
  level: "space" | "project";
  writesDisabled: boolean;
  onRecordChange: (machine: SpaceMachine) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [removing, setRemoving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const edit = async (machine: SpaceMachine, change: WritablePathEdit) => {
    const updated = await updateSpaceMachine(
      machine.machine_id,
      writablePathsRequest(machine, change),
    );
    onRecordChange(updated);
  };

  const remove = async (machine: SpaceMachine, path: string) => {
    setRemoving(path);
    setError(null);
    try {
      await edit(machine, { kind: "remove", path });
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setRemoving(null);
    }
  };

  return (
    <section className="machine-writable-paths" data-machine-writable-paths={level}>
      <header>
        <strong>Writable paths</strong>
        {record && (
          <button
            className="button secondary compact"
            type="button"
            data-machine-action="add-path"
            disabled={writesDisabled || picking}
            onClick={() => {
              setError(null);
              setPicking(true);
            }}
          >
            <FolderPlus size={13} /> Add path
          </button>
        )}
      </header>
      {level === "project" && record && (
        <p className="machine-writable-note">Applies to every project on this machine.</p>
      )}
      {!record ? (
        <p className="machine-writable-note">
          This machine is not in the space list yet. Reload Settings to edit its writable paths.
        </p>
      ) : record.writable_paths.length ? (
        <ul>
          {record.writable_paths.map((path) => (
            <li key={path}>
              <code>{path}</code>
              <button
                className="icon-button"
                type="button"
                data-machine-action="remove-path"
                aria-label={`Remove ${path}`}
                disabled={writesDisabled || removing !== null}
                onClick={() => void remove(record, path)}
              >
                {removing === path ? <LoaderCircle className="spin" size={13} /> : <X size={13} />}
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="machine-writable-note">No extra writable paths.</p>
      )}
      {error && <p role="alert">{error}</p>}
      {record && picking && (
        <PathPicker
          machineId={record.machine_id}
          pickLabel="Add this folder"
          onClose={() => setPicking(false)}
          onPick={async (path) => {
            await edit(record, { kind: "add", path });
            setPicking(false);
          }}
        />
      )}
    </section>
  );
}

/** Adds one machine account to the space list, for setup and project Settings to pick. */
export function NewMachineForm({
  writesDisabled = false,
  onCreated,
  onCancel,
  create = createSpaceMachine,
}: {
  writesDisabled?: boolean;
  onCreated: (machine: SpaceMachine) => void;
  onCancel: () => void;
  create?: (request: SpaceMachineCreateRequest) => Promise<SpaceMachine>;
}) {
  const [draft, setDraft] = useState<SpaceMachineCreateRequest>({
    name: "",
    host: "",
    os_account: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const valid = draft.name.trim() !== "" && draft.host.trim() !== "";
  return (
    <div className="new-machine-form" data-new-machine-form="">
      <label>
        <span>Name</span>
        <input
          value={draft.name}
          maxLength={80}
          placeholder="GPU server"
          onChange={(event) => setDraft({ ...draft, name: event.target.value })}
        />
      </label>
      <label>
        <span>SSH host</span>
        <input
          value={draft.host}
          placeholder="gpu.example.edu"
          onChange={(event) => setDraft({ ...draft, host: event.target.value })}
        />
      </label>
      <label>
        <span>Account</span>
        <input
          value={draft.os_account}
          placeholder="Optional"
          onChange={(event) => setDraft({ ...draft, os_account: event.target.value })}
        />
      </label>
      <div className="new-machine-actions">
        <button
          className="button primary compact"
          type="button"
          data-machine-action="create"
          disabled={writesDisabled || saving || !valid}
          onClick={() =>
            void (async () => {
              setSaving(true);
              setError(null);
              try {
                onCreated(
                  await create({
                    name: draft.name.trim(),
                    host: draft.host.trim(),
                    os_account: draft.os_account.trim(),
                  }),
                );
              } catch (failure) {
                setError(errorMessage(failure));
              } finally {
                setSaving(false);
              }
            })()
          }
        >
          {saving ? <LoaderCircle className="spin" size={13} /> : null}
          Create machine
        </button>
        <button className="button secondary compact" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
