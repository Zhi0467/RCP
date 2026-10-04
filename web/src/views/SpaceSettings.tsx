import {
  ArrowLeft,
  Check,
  HardDrive,
  LoaderCircle,
  Server,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { useState } from "react";
import { clearAllProjectCaches, deleteSpaceMachine, updateMachinePower } from "../api";
import { useMachinePower } from "../hooks/useMachinePower";
import { showMachinePowerCard } from "../machinePower";
import { MachineCard } from "../components/MachineCard";
import { ProviderLogins } from "../components/ProviderLogins";
import { ServerSettings } from "../components/ServerSettings";
import { TranscriptionSettings } from "../components/TranscriptionSettings";
import { ReleaseCheckRow } from "../components/UpdateNotice";
import { errorMessage } from "../errors";
import { useSpaceMachines } from "../hooks/useSpaceMachines";
import { machineHostLabel } from "../spaceMachines";
import type { ProjectCacheMetrics, UpdateNotice } from "../types";

interface Props {
  spaceKind: "personal" | "team";
  updateNotice?: UpdateNotice | null;
  onReleaseCheck?: (notice: UpdateNotice) => void;
  writesDisabled?: boolean;
  /** Any project in the space: the clear-all endpoint is addressed through one. */
  cacheProjectId: string | null;
  cacheClearDisabled: boolean;
  onAllCachesCleared: (projectId: string, metrics: ProjectCacheMetrics) => void;
  onLoginChanged?: () => void;
  onClose: () => void;
}

export function showClearAllCachesWarning(clearStatus: () => void, openWarning: () => void) {
  clearStatus();
  openWarning();
}

/** Settings that belong to the whole space rather than to one project. */
export function SpaceSettings({
  spaceKind,
  updateNotice = null,
  onReleaseCheck,
  writesDisabled = false,
  cacheProjectId,
  cacheClearDisabled,
  onAllCachesCleared,
  onLoginChanged,
  onClose,
}: Props) {
  return (
    <div className="space-settings-shell">
      <header className="space-settings-header">
        <button className="project-back" type="button" onClick={onClose} aria-label="Back">
          <ArrowLeft size={16} />
        </button>
        <h1>Space settings</h1>
      </header>
      <section className="settings-page" data-settings-level="space">
        {spaceKind === "team" ? (
          <ServerSettings onReleaseCheck={onReleaseCheck} />
        ) : (
          <section className="settings-section">
            <ReleaseCheckRow notice={updateNotice} />
          </section>
        )}
        <ThisMac spaceKind={spaceKind} writesDisabled={writesDisabled} />
        <SpaceMachineList spaceKind={spaceKind} writesDisabled={writesDisabled} />
        <ProviderLogins
          spaceKind={spaceKind}
          writesDisabled={writesDisabled}
          onLoginChanged={onLoginChanged}
        />
        <TranscriptionSettings writesDisabled={writesDisabled} />
        {spaceKind === "personal" && cacheProjectId && (
          <ClearAllCaches
            projectId={cacheProjectId}
            disabled={cacheClearDisabled}
            onCleared={onAllCachesCleared}
          />
        )}
      </section>
    </div>
  );
}

function SpaceMachineList({
  spaceKind,
  writesDisabled,
}: {
  spaceKind: "personal" | "team";
  writesDisabled: boolean;
}) {
  const { machines, error, replace, remove } = useSpaceMachines();
  return (
    <section className="settings-section provider-path-settings space-machine-settings">
      <header>
        <span>
          <Server size={16} />
        </span>
        <h2>Machines</h2>
      </header>
      {error && <div className="settings-error">{error}</div>}
      <div className="provider-machine-list">
        {machines === null && !error && <div className="settings-empty">Loading…</div>}
        {machines?.length === 0 && <div className="settings-empty">No machines yet.</div>}
        {machines?.map((machine) => (
          <MachineCard
            key={machine.machine_id}
            title={machine.name}
            hostLabel={machineHostLabel(machine.host, spaceKind)}
            osAccount={machine.os_account}
            record={machine}
            level="space"
            writesDisabled={writesDisabled}
            onRecordChange={replace}
            onDelete={
              machine.in_use === false
                ? async () => {
                    await deleteSpaceMachine(machine.machine_id);
                    remove(machine.machine_id);
                  }
                : undefined
            }
          >
            {machine.projects.length > 0 && (
              <p className="machine-card-projects">
                {machine.projects.map((project) => project.project_name).join(", ")}
              </p>
            )}
          </MachineCard>
        ))}
      </div>
    </section>
  );
}

function ClearAllCaches({
  projectId,
  disabled,
  onCleared,
}: {
  projectId: string;
  disabled: boolean;
  onCleared: (projectId: string, metrics: ProjectCacheMetrics) => void;
}) {
  const [open, setOpen] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [status, setStatus] = useState<{ kind: "saved" | "error"; text: string } | null>(null);

  const clear = async () => {
    if (clearing) return;
    setClearing(true);
    setStatus(null);
    try {
      const result = await clearAllProjectCaches(projectId);
      onCleared(projectId, result);
      setOpen(false);
      const kept = result.project_pages_not_rebuilt;
      setStatus(
        kept.length === 0
          ? { kind: "saved", text: "All project caches cleared." }
          : {
              kind: "error",
              text: `All project caches cleared. These project pages could not be rebuilt and keep their previous copy: ${kept.join(", ")}.`,
            },
      );
    } catch (failure) {
      setStatus({ kind: "error", text: errorMessage(failure) });
    } finally {
      setClearing(false);
    }
  };

  return (
    <section className="settings-section cache-settings space-cache-settings">
      <header>
        <span>
          <HardDrive size={16} />
        </span>
        <h2>Caches</h2>
      </header>
      <div className="app-cache-danger-row">
        <TriangleAlert size={16} aria-hidden="true" />
        <strong>Every project</strong>
        <button
          className="button danger compact"
          type="button"
          disabled={disabled || clearing}
          onClick={() =>
            showClearAllCachesWarning(
              () => setStatus(null),
              () => setOpen(true),
            )
          }
        >
          <Trash2 size={14} /> Clear all project caches
        </button>
      </div>
      {status && !open && (
        <div className={`settings-save-status ${status.kind}`} role="status">
          {status.kind === "saved" ? <Check size={16} /> : <TriangleAlert size={16} />}
          <span>{status.text}</span>
        </div>
      )}
      {open && (
        <div
          className="modal-backdrop"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !clearing) setOpen(false);
          }}
        >
          <section
            className="project-delete-dialog app-cache-clear-dialog"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="app-cache-clear-title"
            aria-describedby="app-cache-clear-warning"
          >
            <header>
              <TriangleAlert size={20} aria-hidden="true" />
              <h2 id="app-cache-clear-title">Clear caches for every project?</h2>
            </header>
            <p id="app-cache-clear-warning">
              Rebuildable remote-source copies and session slices for all projects will be removed.
              Canonical research and original provider data are not affected.
            </p>
            {status?.kind === "error" && (
              <div className="project-delete-error" role="alert">
                {status.text}
              </div>
            )}
            <footer>
              <button
                className="button secondary"
                type="button"
                autoFocus
                disabled={clearing}
                onClick={() => setOpen(false)}
              >
                Cancel
              </button>
              <button
                className="button danger"
                type="button"
                disabled={clearing}
                onClick={() => void clear()}
              >
                {clearing ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
                {clearing ? "Clearing…" : "Clear all project caches"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </section>
  );
}

function ThisMac({
  spaceKind,
  writesDisabled,
}: {
  spaceKind: "personal" | "team";
  writesDisabled: boolean;
}) {
  const { status, setStatus, error: loadError } = useMachinePower(spaceKind);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  if (loadError)
    return (
      <div className="settings-error" role="alert">
        {loadError}
      </div>
    );
  if (!showMachinePowerCard(spaceKind, status) || !status) return null;
  const toggle = async (enabled: boolean) => {
    if (busy || writesDisabled) return;
    setBusy(true);
    setError(null);
    try {
      setStatus(await updateMachinePower({ idle_hold: enabled }));
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setBusy(false);
    }
  };
  const reasons = status.demand_reasons.join(", ");
  return (
    <section className="settings-section provider-path-settings machine-power-settings">
      <header>
        <span>
          <HardDrive size={16} />
        </span>
        <h2>This Mac</h2>
      </header>
      <div className="machine-power-controls">
        <label
          className={
            status.idle_hold.enabled ? "settings-repository selected" : "settings-repository"
          }
        >
          <input
            type="checkbox"
            checked={status.idle_hold.enabled}
            disabled={writesDisabled || busy}
            onChange={(event) => void toggle(event.target.checked)}
          />
          <span className="settings-check">{status.idle_hold.enabled && <Check size={12} />}</span>
          <strong>Stay awake while RCP is working</strong>
        </label>
      </div>
      <p className="machine-power-status" role="status">
        {status.idle_hold.active
          ? `Keeping this Mac awake: ${reasons}`
          : status.idle_hold.enabled
            ? "Nothing running; this Mac may sleep"
            : "Off"}
      </p>
      <p className="machine-power-note">
        Locking the screen is fine. Closing the lid still sleeps this Mac: running work pauses, and
        automatic launches wait until it has been awake for a while.
      </p>
      {error && (
        <div className="settings-error" role="alert">
          {error}
        </div>
      )}
    </section>
  );
}
