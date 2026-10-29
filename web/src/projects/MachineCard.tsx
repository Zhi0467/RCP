import {
  Check,
  FolderPlus,
  KeyRound,
  LoaderCircle,
  Pencil,
  RotateCw,
  ScanSearch,
  ShieldAlert,
  ShieldQuestion,
  Trash2,
  Unlink,
  WifiOff,
  X,
} from "lucide-react";
import { useRef, useState } from "react";
import {
  ApiError,
  checkSpaceMachineCompute,
  createSpaceMachine,
  resolveSpaceMachineProvider,
  trustMachineHostKey,
  updateSpaceMachine,
} from "../core/api";
import { errorMessage } from "../core/errors";
import { useArmedConfirm } from "../core/useArmedConfirm";
import {
  createPathEditor,
  createMachineWriteQueue,
  type MachineSave,
  type WritablePathEdit,
} from "./spaceMachines";
import type {
  MachineLoginFailureCode,
  ProviderId,
  SpaceMachine,
  SpaceMachineCreateRequest,
  SpaceMachineUpdateRequest,
} from "../core/types";
import { MachineProviderSettingRow } from "./MachineProviderSettingRow";
import { MachineBrowserRow } from "./MachineBrowserRow";
import { MachineDependenciesRow } from "./MachineDependenciesRow";
import { PathPicker } from "./PathPicker";
import { Fold } from "../core/Fold";
import { computeProbePresentation } from "../experiments/compute";
import { providerPathPresentation } from "./providerPaths";

interface Props {
  title: string;
  /** Where the account lives, as a person names it. */
  hostLabel: string;
  osAccount: string;
  /** The space record this card edits; null while the space list has not loaded it. */
  record: SpaceMachine | null;
  writesDisabled?: boolean;
  onRecordChange: (machine: SpaceMachine) => void;
  /** Offered for a space machine no repository lives on; it detaches from projects too. */
  onDelete?: () => Promise<void>;
  /** Offered on a project's card when nothing in the project uses the machine. */
  onRemoveFromProject?: () => Promise<void>;
}

/** Runs one card's requests in order; a later answer never lands under an earlier one. */
type MachineRun = <T>(task: () => Promise<T>) => Promise<T>;

/** One machine account, the same card on the space and the project Settings pages. */
export function MachineCard({
  title,
  hostLabel,
  osAccount,
  record,
  writesDisabled = false,
  onRecordChange,
  onDelete,
  onRemoveFromProject,
}: Props) {
  const [busy, setBusy] = useState<"delete" | "unlink" | null>(null);
  const { armed: deleteArmed, arm: armDelete, disarm: disarmDelete } = useArmedConfirm();
  // Every write on this card queues here, so no earlier answer lands after a newer one.
  const writes = useRef(createMachineWriteQueue(updateSpaceMachine)).current;
  const save = writes.save;
  const [failure, setFailure] = useState<FoldFailure | null>(null);
  const act = async (kind: "delete" | "unlink", action: () => Promise<void>) => {
    setBusy(kind);
    setFailure(null);
    try {
      await writes.run(action);
    } catch (caught) {
      setFailure({ key: kind, message: errorMessage(caught) });
    } finally {
      setBusy(null);
    }
  };
  return (
    <article
      className="provider-machine machine-card"
      data-machine-card={record?.machine_id ?? title}
    >
      <header>
        {record ? (
          <MachineName
            record={record}
            writesDisabled={writesDisabled}
            save={save}
            onRenamed={onRecordChange}
          />
        ) : (
          <strong>{title}</strong>
        )}
        <span>
          {hostLabel}
          {osAccount ? ` · ${osAccount}` : ""}
        </span>
        {onRemoveFromProject && (
          <button
            type="button"
            data-machine-action="remove-from-project"
            aria-label="Remove from project"
            title="Remove from project"
            {...failedProps(failure, "unlink")}
            className={`icon-button${failure?.key === "unlink" ? " failed" : ""}`}
            disabled={writesDisabled || busy !== null}
            onClick={() => void act("unlink", onRemoveFromProject)}
          >
            {busy === "unlink" ? <LoaderCircle className="spin" size={14} /> : <Unlink size={14} />}
          </button>
        )}
        {onDelete && (
          <button
            type="button"
            data-machine-action="delete"
            aria-label={deleteArmed ? "Confirm delete" : `Remove ${title}`}
            {...failedProps(failure, "delete")}
            className={`icon-button${deleteArmed ? " armed-delete" : failure?.key === "delete" ? " failed" : ""}`}
            disabled={writesDisabled || busy !== null}
            onBlur={disarmDelete}
            onClick={() => {
              // Delete also detaches the machine from every project, so it arms first.
              if (!deleteArmed) return armDelete();
              disarmDelete();
              void act("delete", onDelete);
            }}
          >
            {busy === "delete" ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
            {deleteArmed && "Delete"}
          </button>
        )}
      </header>
      {record && (
        <>
          {record.projects.length > 0 && (
            <p className="machine-card-projects">
              {[...new Set(record.projects.map((project) => project.project_name))].join(", ")}
            </p>
          )}
          <MachineBrowserRow
            key={`${record.machine_id}:${record.host}:${record.os_account}`}
            machineId={record.machine_id}
            disabled={writesDisabled}
          />
          <MachineDependenciesRow
            key={`${record.machine_id}:${record.host}:${record.os_account}:dependencies`}
            machineId={record.machine_id}
          />
          <ComputeFold
            record={record}
            writesDisabled={writesDisabled}
            save={save}
            run={writes.run}
            onRecordChange={onRecordChange}
          />
          <ProviderExecutables
            record={record}
            writesDisabled={writesDisabled}
            save={save}
            run={writes.run}
            onRecordChange={onRecordChange}
          />
          <Fold
            className="machine-writable-paths"
            title="Provider limits"
            data-machine-fold="provider-limits"
          >
            {record.autocompact_providers.map((option) => (
              <MachineProviderSettingRow
                key={`${record.machine_id}:${option.provider}:autocompact`}
                record={record}
                provider={option.provider}
                setting="provider_autocompact"
                label={`${option.label} auto-compact`}
                placeholder="CLI default"
                hint={`Accepts ${option.hint}. Empty keeps the CLI's default.`}
                writesDisabled={writesDisabled}
                save={save}
                onRecordChange={onRecordChange}
              />
            ))}
            {record.shell_timeout_providers.map((option) => (
              <MachineProviderSettingRow
                key={`${record.machine_id}:${option.provider}:shell-timeout`}
                record={record}
                provider={option.provider}
                setting="provider_shell_timeout"
                label={`${option.label} shell timeout (minutes)`}
                placeholder={String(option.default_minutes)}
                hint="Whole minutes, 2–120. Empty keeps the provider default."
                writesDisabled={writesDisabled}
                save={save}
                onRecordChange={onRecordChange}
              />
            ))}
          </Fold>
          <WritablePaths
            record={record}
            writesDisabled={writesDisabled}
            save={save}
            onRecordChange={onRecordChange}
          />
          <HiddenFolders
            record={record}
            writesDisabled={writesDisabled}
            save={save}
            onRecordChange={onRecordChange}
          />
        </>
      )}
    </article>
  );
}

/** A text field saved on blur or Enter, reset whenever the saved value changes. */
function useSavedText(saved: string, commit: (value: string) => Promise<void>) {
  const [draft, setDraft] = useState(saved);
  const [base, setBase] = useState(saved);
  const [saving, setSaving] = useState(false);
  if (base !== saved) {
    // A newer saved value replaces the draft only when nobody is typing over it.
    setBase(saved);
    if (draft === base || draft.trim() === saved) setDraft(saved);
  }
  const submit = () => {
    const value = draft.trim();
    if (saving) return;
    if (value === saved) {
      setDraft(saved);
      return;
    }
    setSaving(true);
    void commit(value).finally(() => setSaving(false));
  };
  return {
    draft,
    saving,
    inputProps: {
      value: draft,
      onChange: (event: { target: { value: string } }) => setDraft(event.target.value),
      onBlur: submit,
      onKeyDown: (event: { key: string }) => {
        if (event.key === "Enter") submit();
      },
    },
  };
}

interface MachineFoldProps {
  record: SpaceMachine;
  writesDisabled: boolean;
  save: MachineSave;
  run: MachineRun;
  onRecordChange: (machine: SpaceMachine) => void;
}

/** One failed control in a fold: a red edge, its message on hover, and the
 * control itself as the retry. */
interface FoldFailure {
  key: string;
  message: string;
}

function failedProps(failure: FoldFailure | null, key: string) {
  return failure?.key === key
    ? { className: "failed", "aria-invalid": true, title: failure.message }
    : {};
}

/** Saved changes and actions from a fold; a failure marks the control that caused it. */
function useFoldUpdate({ record, save, onRecordChange }: MachineFoldProps) {
  const [failure, setFailure] = useState<FoldFailure | null>(null);
  const attempt = async (key: string, action: () => Promise<void>) => {
    setFailure(null);
    try {
      await action();
    } catch (caught) {
      setFailure({ key, message: errorMessage(caught) });
    }
  };
  const update = (key: string, request: SpaceMachineUpdateRequest) =>
    attempt(key, async () => onRecordChange(await save(record.machine_id, request)));
  return { failure, attempt, update };
}

function ComputeFold(props: MachineFoldProps) {
  const { record, writesDisabled, run, onRecordChange } = props;
  const { failure, attempt, update } = useFoldUpdate(props);
  const [toggling, setToggling] = useState(false);
  const [checking, setChecking] = useState(false);
  const jobsRoot = useSavedText(record.jobs_root, (jobs_root) =>
    update("jobs_root", { jobs_root }),
  );
  const accessHint = useSavedText(record.access_hint, (access_hint) =>
    update("access_hint", { access_hint }),
  );
  const check = async () => {
    setChecking(true);
    await attempt("check", () =>
      run(async () => onRecordChange(await checkSpaceMachineCompute(record.machine_id))),
    );
    setChecking(false);
  };
  // A route check describes the saved settings; an edit not yet saved masks it.
  const unsaved = toggling || jobsRoot.saving || jobsRoot.draft.trim() !== record.jobs_root;
  const probeLine = (route: "scheduler" | "helper") => {
    const probe = unsaved ? null : record.compute_probes[route];
    const presentation = computeProbePresentation(probe);
    return (
      <div className={`compute-probe ${presentation.tone}`} data-compute-route={route}>
        <span className="compute-probe-dot" aria-hidden="true" />
        <span>{presentation.label}</span>
        {probe?.backend_id && <span>{probe.backend_id}</span>}
        {probe?.diagnostic && <span>{probe.diagnostic}</span>}
        {probe?.required_action && <em>{probe.required_action}</em>}
      </div>
    );
  };
  return (
    <Fold
      className="machine-writable-paths machine-compute"
      title="Compute"
      data-machine-fold="compute"
    >
      {/* Slurm only changes what agents are told about this machine; the helper is
          RCP's own launcher, and only it uses the job folder. */}
      <div className="compute-route" data-compute-row="slurm">
        <strong>Slurm</strong>
        <div
          role="group"
          aria-label="Slurm"
          data-machine-setting="job_manager"
          {...failedProps(failure, "job_manager")}
          className={`segmented${failure?.key === "job_manager" ? " failed" : ""}`}
        >
          {(["slurm", "none"] as const).map((job_manager) => (
            <button
              type="button"
              key={job_manager}
              aria-pressed={(record.job_manager === "slurm") === (job_manager === "slurm")}
              disabled={writesDisabled || toggling}
              onClick={() => {
                // Undecided shows as Off; clicking Off still records the choice so no check overrides it.
                if (record.job_manager === job_manager) return;
                setToggling(true);
                void update("job_manager", { job_manager }).finally(() => setToggling(false));
              }}
            >
              {job_manager === "slurm" ? "On" : "Off"}
            </button>
          ))}
        </div>
        {record.job_manager === "slurm" && probeLine("scheduler")}
        <button
          type="button"
          data-machine-action="check-compute"
          aria-label="Check compute again"
          title="Check compute again"
          {...failedProps(failure, "check")}
          className={`icon-button${failure?.key === "check" ? " failed" : ""}`}
          disabled={writesDisabled || checking}
          onClick={() => void check()}
        >
          {checking ? <LoaderCircle className="spin" size={14} /> : <RotateCw size={14} />}
        </button>
      </div>
      <div className="compute-route" data-compute-row="helper">
        <strong>Helper</strong>
        {probeLine("helper")}
        {record.host && (
          <input
            {...jobsRoot.inputProps}
            {...failedProps(failure, "jobs_root")}
            aria-label="Job output folder"
            placeholder="Job folder (~/.rcp/jobs)"
            data-machine-setting="jobs_root"
            disabled={writesDisabled || jobsRoot.saving}
          />
        )}
      </div>
      {record.reach.map(({ from, probe }) => {
        const presentation = computeProbePresentation(probe);
        return (
          <div
            className={`compute-probe compute-reach ${presentation.tone}`}
            key={`reach:${from}`}
            data-compute-reach={from}
          >
            <strong>From {from}</strong>
            <span className="compute-probe-dot" aria-hidden="true" />
            <span>{presentation.label}</span>
            {probe?.required_action ? <em>{probe.required_action}</em> : null}
            {probe && presentation.tone === "error" && !probe.required_action ? (
              <em>{probe.diagnostic}</em>
            ) : null}
          </div>
        );
      })}
      <input
        {...accessHint.inputProps}
        {...failedProps(failure, "access_hint")}
        aria-label="Access hint"
        placeholder="Access hint for agents"
        maxLength={512}
        data-machine-setting="access_hint"
        disabled={writesDisabled || accessHint.saving}
      />
    </Fold>
  );
}

function ProviderExecutables(props: MachineFoldProps) {
  const { record, writesDisabled, run, onRecordChange } = props;
  const { failure, attempt, update } = useFoldUpdate(props);
  const [resolving, setResolving] = useState<ProviderId | null>(null);
  const resolve = async (provider: ProviderId) => {
    setResolving(provider);
    await attempt(`resolve:${provider}`, () =>
      run(async () =>
        onRecordChange(await resolveSpaceMachineProvider(record.machine_id, provider)),
      ),
    );
    setResolving(null);
  };
  return (
    <Fold
      className="machine-writable-paths"
      title="Provider executables"
      data-machine-fold="provider-executables"
    >
      <div className="provider-path-list">
        {record.providers.map(({ provider, label }) => (
          <ProviderPathRow
            key={`${record.machine_id}:${provider}`}
            record={record}
            provider={provider}
            label={label || provider}
            writesDisabled={writesDisabled}
            resolving={resolving}
            failure={failure}
            onSave={(path) => update(`path:${provider}`, { provider_paths: { [provider]: path } })}
            onResolve={() => void resolve(provider)}
          />
        ))}
      </div>
    </Fold>
  );
}

function ProviderPathRow({
  record,
  provider,
  label,
  writesDisabled,
  resolving,
  failure,
  onSave,
  onResolve,
}: {
  record: SpaceMachine;
  provider: ProviderId;
  label: string;
  writesDisabled: boolean;
  resolving: ProviderId | null;
  failure: FoldFailure | null;
  onSave: (path: string) => Promise<void>;
  onResolve: () => void;
}) {
  const recorded = record.provider_paths[provider] ?? "";
  const path = useSavedText(recorded, onSave);
  const state = providerPathPresentation(
    record.provider_readiness[provider],
    path.draft.trim(),
    recorded,
  );
  return (
    <div className="provider-path-row" data-provider={provider}>
      <strong>{label}</strong>
      <input
        type="text"
        {...path.inputProps}
        aria-label={`${label} executable`}
        {...failedProps(failure, `path:${provider}`)}
        data-machine-setting="provider_path"
        disabled={writesDisabled || path.saving}
      />
      <span className={`provider-path-state ${state.kind}`}>{state.label}</span>
      <button
        type="button"
        data-machine-action="resolve-provider"
        {...failedProps(failure, `resolve:${provider}`)}
        className={`button secondary compact${
          failure?.key === `resolve:${provider}` ? " failed" : ""
        }`}
        disabled={writesDisabled || resolving !== null}
        onClick={onResolve}
      >
        {resolving === provider ? (
          <LoaderCircle className="spin" size={14} />
        ) : (
          <ScanSearch size={14} />
        )}
        Resolve
      </button>
    </div>
  );
}

export function WritablePaths({
  record,
  writesDisabled,
  save,
  onRecordChange,
}: {
  record: SpaceMachine;
  writesDisabled: boolean;
  save: MachineSave;
  onRecordChange: (machine: SpaceMachine) => void;
}) {
  const [picking, setPicking] = useState(false);
  // One pending edit at a time, shared by add and remove.
  const [pending, setPending] = useState<WritablePathEdit | null>(null);
  const [error, setError] = useState<string | null>(null);
  const editor = useRef(createPathEditor(save)).current;
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
    <Fold
      className="machine-writable-paths"
      title="Writable paths"
      status={record.writable_paths.length}
      data-machine-writable-paths=""
    >
      <header>
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
      </header>
      {record.writable_paths.length ? (
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
      {picking && (
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
    </Fold>
  );
}

/** Machine additions share the same serialized list editor and folder picker as grants. */
export function HiddenFolders({
  record,
  writesDisabled,
  save,
  onRecordChange,
}: {
  record: SpaceMachine;
  writesDisabled: boolean;
  save: MachineSave;
  onRecordChange: (machine: SpaceMachine) => void;
}) {
  const [picking, setPicking] = useState(false);
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const editor = useRef(createPathEditor(save, "hidden_folders")).current;
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
    <Fold
      className="machine-writable-paths"
      title="Hidden folders"
      status={
        <>
          {(record.hidden_folders ?? []).length}
          {status && status.status !== "enforced" && (
            <span data-hidden-read-status="unhidden" data-tone="bad">
              {" "}
              Unhidden
            </span>
          )}
        </>
      }
      data-machine-hidden-folders=""
    >
      <header>
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
    </Fold>
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

/** A machine card's name, renamed in place. */
function MachineName({
  record,
  writesDisabled,
  save: saveMachine,
  onRenamed,
}: {
  record: SpaceMachine;
  writesDisabled: boolean;
  save: MachineSave;
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
      onRenamed(await saveMachine(record.machine_id, { name }));
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

const LOGIN_FAILURES: Record<MachineLoginFailureCode, { label: string; icon: typeof KeyRound }> = {
  authentication_failed: { label: "SSH login refused", icon: KeyRound },
  host_key_unknown: { label: "Unknown host key", icon: ShieldQuestion },
  host_key_changed: { label: "Host key changed", icon: ShieldAlert },
  unreachable: { label: "Machine unreachable", icon: WifiOff },
};

function loginFailure(
  failure: unknown,
): { code: MachineLoginFailureCode; fingerprint: string | null } | null {
  if (
    !(failure instanceof ApiError) ||
    !failure.code ||
    !Object.hasOwn(LOGIN_FAILURES, failure.code)
  )
    return null;
  const fingerprint = failure.detail?.fingerprint;
  return {
    code: failure.code as MachineLoginFailureCode,
    fingerprint: typeof fingerprint === "string" ? fingerprint : null,
  };
}

/** Adds one machine account to the space list, for setup and project Settings to pick. */
export function NewMachineForm({
  writesDisabled = false,
  accountRequired = false,
  onCreated,
  onCancel,
  create = createSpaceMachine,
  trust = trustMachineHostKey,
}: {
  writesDisabled?: boolean;
  /** Team spaces record each machine's account; a blank one is filled from the server's login. */
  accountRequired?: boolean;
  onCreated: (machine: SpaceMachine) => void;
  onCancel: () => void;
  create?: (request: SpaceMachineCreateRequest) => Promise<SpaceMachine>;
  trust?: (host: string, fingerprint: string) => Promise<unknown>;
}) {
  const [draft, setDraft] = useState<SpaceMachineCreateRequest>({
    name: "",
    host: "",
    os_account: "",
  });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [failure, setFailure] = useState<ReturnType<typeof loginFailure>>(null);
  const valid = draft.name.trim() !== "" && draft.host.trim() !== "";
  const submit = async (before?: () => Promise<unknown>) => {
    setSaving(true);
    setError(null);
    try {
      await before?.();
      setFailure(null);
      onCreated(
        await create({
          name: draft.name.trim(),
          host: draft.host.trim(),
          os_account: draft.os_account.trim(),
        }),
      );
    } catch (caught) {
      const login = loginFailure(caught);
      if (login) setFailure(login);
      else setError(errorMessage(caught));
    } finally {
      setSaving(false);
    }
  };
  const shown = failure ? LOGIN_FAILURES[failure.code] : null;
  const FailureIcon = shown?.icon;
  // Trust is the host-key recovery; any other failure turns Create into one retry.
  const retrying = failure !== null && failure.code !== "host_key_unknown";
  return (
    <div className="new-machine-form" data-new-machine-form="" data-login-failure={failure?.code}>
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
          aria-invalid={failure ? true : undefined}
          onChange={(event) => {
            setFailure(null);
            setDraft({ ...draft, host: event.target.value });
          }}
        />
      </label>
      <label>
        <span>Account</span>
        <input
          value={draft.os_account}
          placeholder={accountRequired ? "From login" : "Optional"}
          onChange={(event) => setDraft({ ...draft, os_account: event.target.value })}
        />
      </label>
      {failure?.code === "host_key_unknown" && failure.fingerprint && FailureIcon && shown && (
        <div className="new-machine-login" role="alert" aria-label={shown.label}>
          <FailureIcon size={14} aria-hidden="true" />
          <code data-host-key-fingerprint="">{failure.fingerprint}</code>
          <button
            className="button secondary compact"
            type="button"
            data-machine-action="trust-host-key"
            disabled={writesDisabled || saving}
            onClick={() => {
              const { fingerprint } = failure;
              void submit(() => trust(draft.host.trim(), fingerprint!));
            }}
          >
            Trust
          </button>
        </div>
      )}
      <div className="new-machine-actions">
        <button
          className={retrying ? "icon-button primary" : "button primary compact"}
          type="button"
          data-machine-action={retrying ? "retry-login" : "create"}
          title={retrying ? shown?.label : undefined}
          aria-label={retrying ? `${shown?.label}. Try again` : undefined}
          disabled={writesDisabled || saving || !valid}
          onClick={() => void submit()}
        >
          {saving ? (
            <LoaderCircle className="spin" size={14} />
          ) : retrying ? (
            <RotateCw size={14} aria-hidden="true" />
          ) : null}
          {retrying ? null : "Create machine"}
        </button>
        <button className="button secondary compact" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
      {error && <p role="alert">{error}</p>}
    </div>
  );
}
