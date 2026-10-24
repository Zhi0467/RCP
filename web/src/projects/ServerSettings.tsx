import {
  ArchiveRestore,
  DatabaseBackup,
  GitCompareArrows,
  RefreshCw,
  ServerCog,
  ShieldCheck,
  TriangleAlert,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { loadServerStatus } from "../core/api";
import { Fold } from "../core/Fold";
import type { ServerStatus, ServerStatusSummary, UpdateNotice } from "../core/types";

interface Props {
  loadStatus?: (refresh: boolean) => Promise<ServerStatus>;
  /** Hands the fresh release check to the app-wide update notice. */
  onReleaseCheck?: (notice: UpdateNotice) => void;
}

export function shortCommit(commit: string | null): string {
  return commit?.slice(0, 10) ?? "Not available";
}

export function formatServerTimestamp(value: string | null): string {
  if (!value) return "Not recorded";
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

export function formatServerBytes(value: number | null): string {
  if (value === null) return "Not recorded";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let amount = value / 1024;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${amount >= 10 ? amount.toFixed(0) : amount.toFixed(1)} ${units[index]}`;
}

export function formatServerProjectCounts(
  protectedProjects: number | null,
  uncapturedProjects: number | null,
): string {
  if (protectedProjects === null || uncapturedProjects === null) return "Not recorded";
  return `${protectedProjects} protected · ${uncapturedProjects} uncaptured`;
}

function StatusMark({ summary }: { summary: ServerStatusSummary }) {
  return <span className={`server-status-mark ${summary.tone}`}>{summary.label}</span>;
}

/** The latest known release, with the check's own state whenever it is not a fresh answer. */
export function latestLabel(version: string | null, check: UpdateNotice["status"]): string {
  const known = version ? `v${version}` : null;
  if (check === "current" || check === "update_available") return known ?? "Not checked";
  const state = check.replaceAll("_", " ");
  return known ? `${known} · ${state}` : state;
}

export function releaseLabel(version: string | null, commit: string | null): string {
  return version ? `v${version}` : shortCommit(commit);
}

function ReleaseRow({
  label,
  version,
  commit,
}: {
  label: string;
  version: string | null;
  commit: string | null;
}) {
  return (
    <div className="server-commit-row">
      <dt>{label}</dt>
      <dd title={commit ?? undefined}>{releaseLabel(version, commit)}</dd>
    </div>
  );
}

export function ServerSettings({ loadStatus = loadServerStatus, onReleaseCheck }: Props) {
  const [status, setStatus] = useState<ServerStatus | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(
    async (refresh: boolean) => {
      setLoading(true);
      setError(null);
      try {
        const loaded = await loadStatus(refresh);
        setStatus(loaded);
        onReleaseCheck?.(loaded.release_check);
      } catch (failure) {
        setStatus(null);
        setError(failure instanceof Error ? failure.message : String(failure));
      } finally {
        setLoading(false);
      }
    },
    [loadStatus, onReleaseCheck],
  );

  useEffect(() => {
    void reload(false);
  }, [reload]);

  return (
    <section className="settings-section server-settings" aria-labelledby="server-settings-title">
      <header>
        <span>
          <ServerCog size={16} />
        </span>
        <h2 id="server-settings-title">Team server</h2>
        {status ? <StatusMark summary={status.overall} /> : null}
        <button
          className="icon-button"
          type="button"
          aria-label="Refresh server status"
          disabled={loading}
          onClick={() => void reload(true)}
        >
          <RefreshCw className={loading ? "spin" : undefined} size={16} />
        </button>
      </header>

      {error ? (
        <div className="server-settings-error" role="alert">
          <TriangleAlert size={16} />
          <span>{error}</span>
        </div>
      ) : null}

      {!status && !error ? (
        <div className="server-settings-loading" aria-live="polite">
          Reading server status…
        </div>
      ) : null}

      {status ? (
        <div className="server-settings-body">
          <Fold
            className="server-release-ledger"
            title={
              <>
                <GitCompareArrows size={16} />
                Release
              </>
            }
            status={<StatusMark summary={status.releases.status} />}
            aria-label="Server release"
          >
            <dl className="server-commit-rail">
              <ReleaseRow
                label="Running"
                version={status.releases.running_version}
                commit={status.releases.running_commit}
              />
              <ReleaseRow
                label="Installed"
                version={status.releases.installed_version}
                commit={status.releases.current_release_commit}
              />
              <div className="server-commit-row">
                <dt>Latest</dt>
                <dd>{latestLabel(status.releases.latest_version, status.release_check.status)}</dd>
              </div>
              <div className="server-commit-row">
                <dt>Checked</dt>
                <dd>{formatServerTimestamp(status.release_check.checked_at)}</dd>
              </div>
              {status.release_check.failure_reason ? (
                <div className="server-commit-row">
                  <dt>Check failed</dt>
                  <dd>{status.release_check.failure_reason}</dd>
                </div>
              ) : null}
              {status.releases.candidate_commit ? (
                <ReleaseRow
                  label="Built update"
                  version={null}
                  commit={status.releases.candidate_commit}
                />
              ) : null}
            </dl>
            {status.releases.last_update_failure ? (
              <p className="server-status-problem">{status.releases.last_update_failure}</p>
            ) : null}
            {status.releases.status.tone !== "good" ? (
              <code>{status.release_check.update_command ?? status.releases.command}</code>
            ) : null}
          </Fold>

          <Fold
            className="server-status-card"
            title={
              <>
                <DatabaseBackup size={16} />
                Protected backup
              </>
            }
            status={<StatusMark summary={status.backup.status} />}
          >
            <dl>
              <div>
                <dt>Last protected</dt>
                <dd>{formatServerTimestamp(status.backup.last_protected_at)}</dd>
              </div>
              <div>
                <dt>Last attempt</dt>
                <dd>{formatServerTimestamp(status.backup.last_attempt_at)}</dd>
              </div>
              <div>
                <dt>Projects</dt>
                <dd>
                  {formatServerProjectCounts(
                    status.backup.protected_projects,
                    status.backup.uncaptured_projects,
                  )}
                </dd>
              </div>
              <div>
                <dt>Captured</dt>
                <dd>{formatServerBytes(status.backup.captured_bytes)}</dd>
              </div>
              <div>
                <dt>Schedule</dt>
                <dd>
                  {status.backup.schedule ?? "Not configured"}
                  {status.backup.retention !== null ? ` · keep ${status.backup.retention}` : ""}
                </dd>
              </div>
              <div className="wide">
                <dt>Destination</dt>
                <dd className="mono">{status.backup.destination ?? "Not configured"}</dd>
              </div>
            </dl>
            {status.backup.last_failure ? (
              <p className="server-status-problem">{status.backup.last_failure}</p>
            ) : null}
            <div className="server-status-commands">
              <code>{status.backup.configure_command}</code>
              <code>{status.backup.run_command}</code>
            </div>
          </Fold>

          <Fold
            className="server-status-card"
            title={
              <>
                <ArchiveRestore size={16} />
                Restore drill
              </>
            }
            status={<StatusMark summary={status.restore.status} />}
          >
            <dl>
              <div>
                <dt>Completed</dt>
                <dd>{formatServerTimestamp(status.restore.last_completed_at)}</dd>
              </div>
              <div>
                <dt>Age</dt>
                <dd>
                  {status.restore.drill_age_days === null
                    ? "Not recorded"
                    : `${status.restore.drill_age_days} days`}
                </dd>
              </div>
            </dl>
            <code>{status.restore.command}</code>
          </Fold>

          <Fold
            className="server-status-card"
            title={
              <>
                <ShieldCheck size={16} />
                Execution readiness
              </>
            }
            status={
              <span className="server-readiness-list">
                <StatusMark summary={status.execution.machine} />
                <StatusMark summary={status.execution.provider_checks} />
              </span>
            }
          >
            <p className="server-dependency-versions mono">
              {status.execution.dependency_versions}
            </p>
            <code>{status.execution.provider_command}</code>
          </Fold>

          {status.problems.length > 0 ? (
            <Fold
              className="server-problem-ledger"
              title={
                <>
                  <TriangleAlert size={16} />
                  Needs attention
                </>
              }
              status={<span data-tone="bad">{status.problems.length}</span>}
            >
              <ul>
                {status.problems.map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
            </Fold>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
