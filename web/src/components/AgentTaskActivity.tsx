import {
  AlertTriangle,
  CheckCircle2,
  CirclePause,
  Eye,
  LoaderCircle,
  Play,
  RotateCcw,
  X,
} from "lucide-react";
import { isActiveTask, taskKindLabel } from "../agentTasks";
import type { AgentTask } from "../types";

interface Props {
  task: AgentTask;
  actionBusy: boolean;
  mutatingActionsDisabled?: boolean;
  onPause: () => void;
  onResume: () => void;
  onRetry: () => void;
  onInspect: () => void;
  onDismiss: () => void;
}

export function AgentTaskActivity({
  task,
  actionBusy,
  mutatingActionsDisabled = false,
  onPause,
  onResume,
  onRetry,
  onInspect,
  onDismiss,
}: Props) {
  const active = isActiveTask(task);
  const succeeded = task.status === "succeeded";
  const paused = task.status === "paused";
  const percent = Math.round(task.progress * 100);

  return (
    <section
      className={`run-activity agent-task-strip ${active ? "active" : succeeded ? "succeeded" : paused ? "paused" : "failed"}`}
      aria-live={active ? "polite" : "assertive"}
      aria-label={`Agent task: ${taskKindLabel(task.kind)}`}
    >
      <div className="run-activity-icon">
        {active ? (
          <LoaderCircle className="spin" size={18} />
        ) : succeeded ? (
          <CheckCircle2 size={19} />
        ) : paused ? (
          <CirclePause size={18} />
        ) : (
          <AlertTriangle size={19} />
        )}
      </div>
      <div className="run-activity-copy">
        <div className="run-activity-heading">
          <strong>
            {succeeded ? "Done — " : ""}
            {taskKindLabel(task.kind)}
          </strong>
          <span>{taskStatusLabel(task)}</span>
        </div>
        <p>{task.error || task.status_message}</p>
        {active && (
          <div className="run-progress-row">
            <div
              className="run-progress-track"
              role="progressbar"
              aria-label="Estimated agent progress"
              aria-valuemin={0}
              aria-valuemax={100}
              aria-valuenow={percent}
            >
              <span style={{ width: `${percent}%` }} />
            </div>
            <span className="mono">{percent}%</span>
            <span>{estimateLabel(task)}</span>
          </div>
        )}
      </div>
      <div className="run-activity-actions">
        {task.can_pause && (
          <button className="button compact secondary" disabled={actionBusy} onClick={onPause}>
            <CirclePause size={13} /> Pause
          </button>
        )}
        {task.can_resume && (
          <button
            className="button compact primary"
            disabled={actionBusy || mutatingActionsDisabled}
            onClick={onResume}
          >
            <Play size={13} /> Resume
          </button>
        )}
        {task.can_retry && (
          <button
            className="button compact secondary"
            disabled={actionBusy || mutatingActionsDisabled}
            onClick={onRetry}
          >
            <RotateCcw size={13} />{" "}
            {task.kind === "seed" || task.kind === "refresh" ? "Retry…" : "Retry"}
          </button>
        )}
        <button className="button compact ghost" onClick={onInspect}>
          <Eye size={13} /> Inspect
        </button>
        {!active && (
          <button
            className="icon-button compact"
            aria-label="Dismiss agent task notification"
            onClick={onDismiss}
          >
            <X size={14} />
          </button>
        )}
      </div>
    </section>
  );
}

export function taskStatusLabel(task: AgentTask): string {
  switch (task.status) {
    case "queued":
      return "Queued";
    case "running":
      return "Running in the background";
    case "pausing":
      return "Pausing";
    case "paused":
      return "Paused at checkpoint";
    case "succeeded":
      return task.applied_revision ? `Completed at revision ${task.applied_revision}` : "Completed";
    case "interrupted":
      return "Interrupted";
    default:
      return "Failed";
  }
}

export function estimateLabel(task: AgentTask): string {
  if (task.status === "succeeded") return formatDuration(task.elapsed_seconds);
  if (task.status === "queued") return "Waiting for worker";
  if (task.elapsed_seconds > task.estimate_seconds) return "Longer than estimate";
  return `about ${formatDuration(Math.max(0, task.estimate_seconds - task.elapsed_seconds))} left`;
}

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.max(1, Math.round(seconds))}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}
