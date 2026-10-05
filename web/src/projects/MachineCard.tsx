import { Check, FolderPlus, LoaderCircle, Pencil, Trash2, X } from "lucide-react";
import { useRef, useState, type ReactNode } from "react";
import { createSpaceMachine, updateSpaceMachine } from "../core/api";
import { errorMessage } from "../core/errors";
import { createPathEditor, type WritablePathEdit } from "./spaceMachines";
import type { SpaceMachine, SpaceMachineCreateRequest } from "../core/types";
import { MachineBrowserRow } from "./MachineBrowserRow";
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
        {level === "space" && record ? (
          <MachineName record={record} writesDisabled={writesDisabled} onRenamed={onRecordChange} />
        ) : (
          <strong>{title}</strong>
        )}
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
      {record && (
        <MachineBrowserRow
          key={`${record.machine_id}:${record.host}:${record.os_account}`}
          machineId={record.machine_id}
          disabled={writesDisabled}
        />
      )}
      <WritablePaths
        record={record}
        level={level}
        writesDisabled={writesDisabled}
        onRecordChange={onRecordChange}
      />
      {record && (
        <HiddenFolders
          record={record}
          writesDisabled={writesDisabled}
          onRecordChange={onRecordChange}
        />
      )}
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
  // One pending edit at a time, shared by add and remove.
  const [pending, setPending] = useState<WritablePathEdit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const editor = useRef(createPathEditor(updateSpaceMachine)).current;
  editor.sync(record);

  const edit = async (change: WritablePathEdit) => {
    setPending(change);
    try {
      onRecordChange(await editor.edit(change));
    } finally {
      setPending(null);
    }
  };

  const remove = async (path: string) => {
    if (editor.pending) return;
    setError(null);
    try {
      await edit({ kind: "remove", path });
    } catch (failure) {
      setError(errorMessage(failure));
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
            disabled={writesDisabled || picking || pending !== null}
            onClick={() => {
              setError(null);
              setPicking(true);
            }}
          >
            <FolderPlus size={14} /> Add path
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
                disabled={writesDisabled || pending !== null}
                onClick={() => void remove(path)}
              >
                {pending?.kind === "remove" && pending.path === path ? (
                  <LoaderCircle className="spin" size={14} />
                ) : (
                  <X size={14} />
                )}
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
            await edit({ kind: "add", path });
            setPicking(false);
          }}
        />
      )}
    </section>
  );
}

/** Machine additions share the same serialized list editor and folder picker as grants. */
export function HiddenFolders({
  record,
  writesDisabled,
  onRecordChange,
}: {
  record: SpaceMachine;
  writesDisabled: boolean;
  onRecordChange: (machine: SpaceMachine) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editor = useRef(createPathEditor(updateSpaceMachine, "hidden_folders")).current;
  editor.sync(record);
  const projection = record.hidden_read;
  const defaults = projection?.default_paths ?? [];
  const status = projection?.readiness;
  const edit = async (change: WritablePathEdit) => {
    setPending(true);
    setError(null);
    try {
      onRecordChange(await editor.edit(change));
    } finally {
      setPending(false);
    }
  };
  return (
    <section className="machine-writable-paths" data-machine-hidden-folders="">
      <header>
        <strong>Hidden folders</strong>
        <button
          type="button"
          className="button secondary compact"
          data-machine-action="add-hidden-folder"
          disabled={writesDisabled || pending || picking}
          onClick={() => setPicking(true)}
        >
          <FolderPlus size={14} /> Add folder
        </button>
      </header>
      {status ? (
        <div role="status" data-hidden-read-status={status.status}>
          <strong>{status.status === "enforced" ? "Enforced" : "Unhidden"}</strong>
          {status.reasons.length > 0 && (
            <ul>
              {status.reasons.map((reason) => (
                <li key={reason} data-hidden-read-reason={reason}>
                  {hiddenReadReason(reason)}
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <p role="status" data-hidden-read-status="unchecked">
          Checked at launch
        </p>
      )}
      {defaults.length > 0 && (
        <ul aria-label="Default hidden paths">
          {defaults.map((path) => (
            <li key={path} data-hidden-default="">
              <code>{path}</code>
              <span>Default</span>
            </li>
          ))}
        </ul>
      )}
      <ul aria-label="Added hidden folders">
        {(record.hidden_folders ?? []).map((path) => (
          <li key={path}>
            <code>{path}</code>
            <button
              type="button"
              className="icon-button"
              aria-label={`Remove ${path}`}
              data-machine-action="remove-hidden-folder"
              disabled={writesDisabled || pending}
              onClick={() => {
                void edit({ kind: "remove", path }).catch((failure) =>
                  setError(errorMessage(failure)),
                );
              }}
            >
              <X size={14} />
            </button>
          </li>
        ))}
      </ul>
      {error && <p role="alert">{error}</p>}
      {picking && (
        <PathPicker
          machineId={record.machine_id}
          pickLabel="Hide this folder"
          onClose={() => setPicking(false)}
          onPick={async (path) => {
            await edit({ kind: "add", path });
            setPicking(false);
          }}
        />
      )}
    </section>
  );
}

function hiddenReadReason(reason: string): string {
  const reasons: Record<string, string> = {
    wrapper_unavailable: "This host cannot enforce secret hiding.",
    userns_blocked: "This host blocks the Linux isolation needed to hide secrets.",
    provider_native_tools_uncovered: "This provider's file tools cannot enforce secret hiding.",
    browser_unwrapped_macos: "Browser file operations remain unhidden on macOS.",
    deploy_key_agent_unconfirmed:
      "Deploy keys remain readable until SSH-agent signing is confirmed.",
    ssh_key_agent_unconfirmed: "SSH keys remain readable until SSH-agent signing is confirmed.",
    credential_compatibility_exception: "Credentials required by tools remain readable.",
    hidden_folder_conflict:
      "A hidden folder now covers a path tools need, so that folder stays readable.",
  };
  return reasons[reason] ?? reason;
}

/** Adds one machine account to the space list, for setup and project Settings to pick. */
/** A space card's name, renamed in place; a project names its machines itself. */
function MachineName({
  record,
  writesDisabled,
  onRenamed,
}: {
  record: SpaceMachine;
  writesDisabled: boolean;
  onRenamed: (machine: SpaceMachine) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (draft === null) {
    return (
      <>
        <strong>{record.name}</strong>
        <button
          className="icon-button"
          type="button"
          data-machine-action="rename"
          aria-label={`Rename ${record.name}`}
          disabled={writesDisabled}
          onClick={() => setDraft(record.name)}
        >
          <Pencil size={14} />
        </button>
      </>
    );
  }
  const name = draft.trim();
  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      onRenamed(await updateSpaceMachine(record.machine_id, { name }));
      setDraft(null);
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setSaving(false);
    }
  };
  return (
    <span className="machine-name-edit">
      <input
        value={draft}
        maxLength={80}
        aria-label="Machine name"
        autoFocus
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && name) void save();
          if (event.key === "Escape") setDraft(null);
        }}
      />
      <button
        className="icon-button"
        type="button"
        data-machine-action="save-name"
        aria-label="Save name"
        disabled={writesDisabled || saving || !name}
        onClick={() => void save()}
      >
        {saving ? <LoaderCircle className="spin" size={14} /> : <Check size={14} />}
      </button>
      <button
        className="icon-button"
        type="button"
        aria-label="Cancel rename"
        disabled={saving}
        onClick={() => setDraft(null)}
      >
        <X size={14} />
      </button>
      {error && <em role="alert">{error}</em>}
    </span>
  );
}

export function NewMachineForm({
  writesDisabled = false,
  accountRequired = false,
  onCreated,
  onCancel,
  create = createSpaceMachine,
}: {
  writesDisabled?: boolean;
  /** Team spaces back up each machine's account, so the server requires it. */
  accountRequired?: boolean;
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
  const valid =
    draft.name.trim() !== "" &&
    draft.host.trim() !== "" &&
    (!accountRequired || draft.os_account.trim() !== "");
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
          placeholder={accountRequired ? "Required" : "Optional"}
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
          {saving ? <LoaderCircle className="spin" size={14} /> : null}
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
