import { FlaskConical } from "lucide-react";
import type { ExperimentLoopHealth, ExperimentRun, ExperimentWatcherGroup } from "../runProjection";
import type { WatcherRecord } from "../types";

const healthLabels: Record<ExperimentLoopHealth, string> = {
  starting: "Starting",
  agent_active: "Agent active",
  waiting_on_watchers: "Waiting on watchers",
  degraded: "Watcher degraded",
  stopping: "Stopping gracefully",
  human_stopped: "Human-stopped",
  paused_at_limit: "Paused at invocation limit",
  needs_action: "Needs action",
  completed: "Completed",
};

const healthTones: Record<ExperimentLoopHealth, string> = {
  starting: "running",
  agent_active: "running",
  waiting_on_watchers: "waiting",
  degraded: "degraded",
  stopping: "stopping",
  human_stopped: "stopped",
  paused_at_limit: "paused",
  needs_action: "actionable",
  completed: "completed",
};

const liveTaskStatuses = new Set(["queued", "running", "pausing"]);

export function experimentHealthLabel(health: ExperimentLoopHealth): string {
  return healthLabels[health];
}

export function experimentHealthTone(health: ExperimentLoopHealth): string {
  return healthTones[health];
}

export function experimentLoopIsLive(run: ExperimentRun): boolean {
  const operational = run.control?.operational;
  return Boolean(
    (run.currentTask && liveTaskStatuses.has(run.currentTask.status)) ||
    operational?.task_active ||
    operational?.detached_work_active ||
    operational?.watcher_completion_pending ||
    run.currentWatchers.some(
      (watcher) =>
        watcher.status === "active" ||
        watcher.status === "degraded" ||
        (watcher.status === "completed" && !watcher.notified),
    ),
  );
}

export function experimentStopUnsettled(run: ExperimentRun): boolean {
  const operational = run.control?.operational;
  return Boolean(operational?.stop_requested && !operational.stop_settled);
}

interface Props {
  run: ExperimentRun;
  runBusy: boolean;
  runDisabled: boolean;
  stopBusy: boolean;
  onRun: () => void;
  onStopLoop: () => void;
  onInspectTask: (operationId: string) => void;
}

export function ExperimentRunDetail({
  run,
  runBusy,
  runDisabled,
  stopBusy,
  onRun,
  onStopLoop,
  onInspectTask,
}: Props) {
  const { node, control, taskGroup, currentTask, watchers, health } = run;
  const operational = control?.operational ?? null;
  const session = operational?.session ?? null;
  const live = experimentLoopIsLive(run);
  const stopUnsettled = experimentStopUnsettled(run);
  const stopRequested = Boolean(operational?.stop_requested);
  const taskInFlight = Boolean(currentTask && liveTaskStatuses.has(currentTask.status));
  const currentOperationId =
    operational?.current_operation_id ??
    currentTask?.operation_id ??
    taskGroup?.latest.operation_id;
  const attempts = node.attempts ?? [];
  const completionCriteria = node.completion_criteria ?? [];

  return (
    <div className={`experiment-run-detail ${healthTones[health]}`}>
      <div className="experiment-run-topline">
        <span
          className={`experiment-run-health ${healthTones[health]}`}
          role="status"
          aria-live="polite"
          aria-atomic="true"
        >
          <span className="eyebrow">Loop health</span>
          <strong>{healthLabels[health]}</strong>
        </span>
        <div className="experiment-run-actions" aria-label="Experiment loop actions">
          {currentOperationId && (
            <button
              type="button"
              className="button compact"
              onClick={() => onInspectTask(currentOperationId)}
            >
              Open agent task
            </button>
          )}
          {control?.episode_id && (
            <button
              type="button"
              className="button compact experiment-stop-loop"
              disabled={stopBusy || stopUnsettled || stopRequested || !live}
              onClick={onStopLoop}
            >
              {stopBusy || stopUnsettled
                ? "Stopping"
                : stopRequested
                  ? "Loop stopped"
                  : "Stop loop"}
            </button>
          )}
          <button
            type="button"
            className="button primary compact experiment-run-button"
            disabled={runDisabled || runBusy || taskInFlight || stopUnsettled || !control?.ready}
            onClick={onRun}
            aria-describedby={control?.reasons.length ? `${node.id}-run-requirements` : undefined}
          >
            <FlaskConical size={13} aria-hidden="true" />{" "}
            {runBusy ? "Starting" : health === "paused_at_limit" ? "Run pending wake" : "Run"}
          </button>
        </div>
      </div>

      <dl className="experiment-run-facts experiment-run-primary-facts">
        <div>
          <dt>Now</dt>
          <dd>{nowLine(run)}</dd>
        </div>
        <div>
          <dt>Phase</dt>
          <dd>{currentTask?.phase || operational?.current_phase || "—"}</dd>
        </div>
        <div>
          <dt>Last activity</dt>
          <dd>
            {formatMoment(currentTask?.last_activity_at ?? operational?.current_last_activity_at)}
          </dd>
        </div>
        <div>
          <dt>Episode</dt>
          <dd className="mono">{control?.episode_id ?? "—"}</dd>
        </div>
        <div>
          <dt>Invocation budget</dt>
          <dd>
            {control ? `${control.invocations_used} / ${control.invocation_ceiling}` : "—"}
            {control ? ` · ${control.invocations_remaining} remaining` : ""}
          </dd>
        </div>
        <div>
          <dt>Current invocation</dt>
          <dd>{operational?.current_invocation ?? taskInvocation(currentTask) ?? "—"}</dd>
        </div>
      </dl>

      {control && control.reasons.length > 0 && (
        <ul
          id={`${node.id}-run-requirements`}
          className="experiment-gate-reasons"
          aria-label="Run requirements"
        >
          {control.reasons.map((reason) => (
            <li key={reason}>{reason}</li>
          ))}
        </ul>
      )}

      <section className="experiment-run-block experiment-run-watchers-block">
        <div className="experiment-run-block-heading">
          <h4>Watchers</h4>
          <span>{watchers.length}</span>
        </div>
        {watchers.length === 0 ? (
          <p className="experiment-run-empty">No detached work has been handed off.</p>
        ) : (
          <ul className="experiment-run-watchers" aria-label="Experiment watchers">
            {run.watcherItems.map((item) =>
              item.kind === "group" ? (
                <WatcherGroupDetail group={item.group} key={item.group.groupId} />
              ) : (
                <WatcherDetail watcher={item.watcher} key={item.watcher.watcher_id} />
              ),
            )}
          </ul>
        )}
      </section>

      <section className="experiment-run-block">
        <div className="experiment-run-block-heading">
          <h4>Execution</h4>
        </div>
        <dl className="experiment-run-facts">
          <div>
            <dt>Provider</dt>
            <dd>{session?.provider ?? "—"}</dd>
          </div>
          <div>
            <dt>Model</dt>
            <dd>{session?.model || "Provider default"}</dd>
          </div>
          <div>
            <dt>Reasoning</dt>
            <dd>{session?.reasoning ?? "—"}</dd>
          </div>
          <div>
            <dt>Machine</dt>
            <dd>
              {session?.run_on ?? "—"}
              {session?.execution_host ? ` · ${session.execution_host}` : ""}
            </dd>
          </div>
          <div>
            <dt>Truth scope</dt>
            <dd className="experiment-run-breakable">
              {session?.run_truth_scope?.join(", ") || "—"}
            </dd>
          </div>
          <div>
            <dt>Native continuity</dt>
            <dd>
              {session?.native_session_bound ? "Bound" : "Not bound"}
              {session?.diagnostic ? ` · ${session.diagnostic}` : ""}
            </dd>
          </div>
          <div>
            <dt>Current task</dt>
            <dd className="mono experiment-run-breakable">{currentOperationId ?? "—"}</dd>
          </div>
        </dl>
      </section>

      <section className="experiment-run-block">
        <div className="experiment-run-block-heading">
          <h4>Experiment meaning</h4>
        </div>
        <dl className="experiment-run-facts">
          <div>
            <dt>Status</dt>
            <dd>{node.status ?? "—"}</dd>
          </div>
          <div>
            <dt>Current summary</dt>
            <dd>{String(node.current_summary || node.objective || "—")}</dd>
          </div>
          <div>
            <dt>Next action</dt>
            <dd>{String(node.next_action || "—")}</dd>
          </div>
        </dl>

        {completionCriteria.length > 0 && (
          <div className="experiment-run-subsection">
            <h5>Completion criteria</h5>
            <ul>
              {completionCriteria.map((criterion) => (
                <li key={criterion}>{criterion}</li>
              ))}
            </ul>
          </div>
        )}

        {attempts.length > 0 && (
          <div className="experiment-run-subsection">
            <h5>Semantic attempts</h5>
            <ol className="experiment-run-attempts" aria-label="Semantic attempts">
              {attempts.map((attempt) => (
                <li key={attempt.id}>
                  <span className="experiment-run-attempt-seq">
                    {String(attempt.sequence).padStart(2, "0")}
                  </span>
                  <span className="experiment-run-attempt-copy">
                    <strong>{attempt.purpose}</strong>
                    <span>
                      {attempt.outcome || attempt.failure_reason || "No outcome recorded"}
                    </span>
                    {attempt.job_refs.length > 0 && (
                      <span className="mono experiment-run-breakable">
                        {attempt.job_refs.join(", ")}
                      </span>
                    )}
                  </span>
                  <span className={`status-pill ${attempt.status}`}>{attempt.status}</span>
                </li>
              ))}
            </ol>
          </div>
        )}

        {(control?.governing_decisions ?? []).length > 0 && (
          <div className="experiment-run-subsection">
            <h5>Governing decisions</h5>
            <ul className="experiment-run-decisions">
              {(control?.governing_decisions ?? []).map((pin) => (
                <li key={pin.decision_id}>
                  <span className="mono">{pin.decision_id}</span> · r{pin.decision_revision} ·{" "}
                  {pin.selected_option}
                </li>
              ))}
            </ul>
          </div>
        )}

        {(control?.decision_drift ?? []).length > 0 && (
          <div className="experiment-run-subsection experiment-run-drift">
            <h5>Decision drift</h5>
            <ul>
              {(control?.decision_drift ?? []).map((drift) => (
                <li key={drift.decision_id}>
                  {drift.proposed
                    ? `${drift.decision_id} has a proposed change. This episode was pinned to ${drift.pinned_option}.`
                    : `${drift.decision_id} moved to ${drift.current_option ?? drift.current_status ?? "an unavailable state"} after this episode was pinned to ${drift.pinned_option}.`}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </div>
  );
}

function WatcherGroupDetail({ group }: { group: ExperimentWatcherGroup }) {
  return (
    <li className="experiment-run-watcher-group">
      <details>
        <summary>
          <span>
            <span className="eyebrow">Watcher group</span>
            <strong>{group.label}</strong>
          </span>
          <span className="experiment-run-watcher-group-counts">{watcherGroupSummary(group)}</span>
        </summary>
        <p className="experiment-run-watcher-group-id">
          <span className="eyebrow">Group ID</span>
          <code>{group.groupId}</code>
        </p>
        <ul className="experiment-run-watcher-group-members" aria-label={`${group.label} watchers`}>
          {group.watchers.map((watcher) => (
            <WatcherDetail watcher={watcher} key={watcher.watcher_id} />
          ))}
        </ul>
      </details>
    </li>
  );
}

function WatcherDetail({ watcher }: { watcher: WatcherRecord }) {
  return (
    <li className={`experiment-run-watcher ${watcher.status}`}>
      <div className="experiment-run-watcher-heading">
        <span className={`status-pill ${watcher.status}`}>{watcher.status}</span>
        <strong className="mono experiment-run-breakable">{watcher.watcher_id}</strong>
        <span>{watcherDeliveryLabel(watcher)}</span>
      </div>
      <dl className="experiment-run-watcher-facts">
        <div>
          <dt>Origin invocation</dt>
          <dd className="mono experiment-run-breakable">{watcher.origin_operation_id}</dd>
        </div>
        <div>
          <dt>Provenance</dt>
          <dd>{watcherProvenance(watcher)}</dd>
        </div>
        <div>
          <dt>Created</dt>
          <dd>{formatMoment(watcher.created_at)}</dd>
        </div>
        <div>
          <dt>Last check</dt>
          <dd>{formatMoment(watcher.last_checked_at)}</dd>
        </div>
        <div>
          <dt>Exit code</dt>
          <dd>{watcher.last_exit_code ?? "—"}</dd>
        </div>
        <div>
          <dt>Current error</dt>
          <dd className={watcher.last_error ? "experiment-run-watcher-current-error" : undefined}>
            {watcher.last_error ?? "—"}
          </dd>
        </div>
        <div>
          <dt>Completed</dt>
          <dd>{formatMoment(watcher.completed_at)}</dd>
        </div>
        <div>
          <dt>Machine</dt>
          <dd>{watcher.execution_host || "Local"}</dd>
        </div>
        <div>
          <dt>Delivery task</dt>
          <dd className="mono experiment-run-breakable">
            {watcher.notification_operation_id ?? "—"}
          </dd>
        </div>
        {watcher.status === "stopped" && (
          <div>
            <dt>Stopped by</dt>
            <dd>{watcherStopDisposition(watcher)}</dd>
          </div>
        )}
      </dl>
      <div className="experiment-run-watcher-command">
        <span className="eyebrow">Check command</span>
        <code>{watcher.check_command}</code>
      </div>
      <div className="experiment-run-watcher-paths">
        <span>
          <span className="eyebrow">Log</span>
          <code>{watcher.log_path}</code>
        </span>
        <span>
          <span className="eyebrow">Working directory</span>
          <code>{watcher.cwd}</code>
        </span>
      </div>
      {watcher.stop_reason && (
        <p className="experiment-run-watcher-stop-reason">
          <strong>{watcher.stopped_by === "agent" ? "Agent reason" : "Stop reason"}</strong>
          {watcher.stop_reason}
        </p>
      )}
    </li>
  );
}

function nowLine(run: ExperimentRun): string {
  const operational = run.control?.operational ?? null;
  const taskMessage = run.currentTask?.status_message || operational?.current_status_message || "";
  const liveWatchers = run.currentWatchers.filter(
    (watcher) => watcher.status === "active" || watcher.status === "degraded",
  ).length;
  switch (run.health) {
    case "starting":
    case "agent_active":
      return taskMessage || "Agent turn in flight";
    case "stopping":
      return taskMessage || "Stop requested — the current turn finishes first";
    case "human_stopped":
      return "Stopped — no watcher can wake this episode";
    case "paused_at_limit":
      return "Waiting for you to authorize another invocation";
    case "degraded":
      return "A watcher check is failing";
    case "waiting_on_watchers":
      return liveWatchers > 0
        ? `Waiting on ${liveWatchers} detached ${liveWatchers === 1 ? "watcher" : "watchers"}`
        : "Waiting on detached watchers";
    case "completed":
      return `Experiment ${String(run.node.status ?? "completed")}`;
    default:
      return (
        run.currentTask?.error ||
        operational?.session.diagnostic ||
        taskMessage ||
        "No invocation is running"
      );
  }
}

function watcherDeliveryLabel(watcher: WatcherRecord): string {
  if (watcher.notification_operation_id) return "Delivery claimed";
  if (watcher.status === "stopped") return "Stopped · not delivered";
  if (watcher.status === "completed" && !watcher.notified) return "Pending delivery";
  if (watcher.notified) return "Acknowledged · not delivered";
  return "Not delivered";
}

function watcherGroupSummary(group: ExperimentWatcherGroup): string {
  const { finished, degraded, running, stopped } = group.counts;
  const summary = [`${finished} finished`, `${degraded} degraded`, `${running} running`];
  if (stopped > 0) summary.push(`${stopped} stopped`);
  return summary.join(" · ");
}

function watcherProvenance(watcher: WatcherRecord): string {
  const episode = watcher.continuation.control_episode_id ?? "—";
  const invocation = watcher.continuation.control_invocation ?? "—";
  const ceiling = watcher.continuation.control_invocation_ceiling;
  return `episode ${episode} · invocation ${invocation}${ceiling ? ` / ${ceiling}` : ""}`;
}

function watcherStopDisposition(watcher: WatcherRecord): string {
  const actor = watcher.stopped_by ? `${capitalize(watcher.stopped_by)} stopped` : "Stopped";
  const stoppedAt = formatMoment(watcher.stopped_at);
  return stoppedAt === "—" ? actor : `${actor} · ${stoppedAt}`;
}

function capitalize(value: string): string {
  return `${value[0].toUpperCase()}${value.slice(1)}`;
}

function taskInvocation(task: ExperimentRun["currentTask"]): number | null {
  const value = task?.request.control_invocation;
  return typeof value === "number" && Number.isInteger(value) ? value : null;
}

function formatMoment(value: string | null | undefined): string {
  if (!value || !Number.isFinite(Date.parse(value))) return "—";
  return new Date(value).toLocaleString();
}
