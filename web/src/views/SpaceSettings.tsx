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
import { clearAllProjectCaches, deleteSpaceMachine } from "../api";
import { MachineCard } from "../components/MachineCard";
import { ProviderLogins } from "../components/ProviderLogins";
import { ServerSettings } from "../components/ServerSettings";
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
        <SpaceMachineList spaceKind={spaceKind} writesDisabled={writesDisabled} />
        <ProviderLogins
          spaceKind={spaceKind}
          writesDisabled={writesDisabled}
          onLoginChanged={onLoginChanged}
        />
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
      onCleared(projectId, await clearAllProjectCaches(projectId));
      setOpen(false);
      setStatus({ kind: "saved", text: "All project caches cleared." });
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
          <Trash2 size={13} /> Clear all project caches
        </button>
      </div>
      {status && !open && (
        <div className={`settings-save-status ${status.kind}`} role="status">
          {status.kind === "saved" ? <Check size={15} /> : <TriangleAlert size={15} />}
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
              <TriangleAlert size={18} aria-hidden="true" />
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
                {clearing ? <LoaderCircle className="spin" size={13} /> : <Trash2 size={13} />}
                {clearing ? "Clearing…" : "Clear all project caches"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </section>
  );
}
