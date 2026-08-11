import { Clock3, X } from "lucide-react";
import { taskKindLabel, taskStatusLabel } from "../agentTasks";
import type { AgentTask, RevisionSummary } from "../types";

interface Props {
  summaries: RevisionSummary[];
  tasks: AgentTask[];
  loading: boolean;
  error: string | null;
  onInspectTask: (taskId: string) => void;
  onClose: () => void;
}

export function ProjectHistoryDrawer({
  summaries,
  tasks,
  loading,
  error,
  onInspectTask,
  onClose,
}: Props) {
  const newestFirst = [...summaries].sort((left, right) => right.to_revision - left.to_revision);

  return (
    <div
      className="drawer-scrim"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside className="detail-drawer run-inspector" aria-label="Project history">
        <header>
          <h2>Project history</h2>
          <button className="icon-button" aria-label="Close project history" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="run-inspector-body">
          <nav className="run-history" aria-label="Agent tasks">
            <span className="eyebrow">Agent tasks</span>
            {tasks.map((task) => (
              <button
                data-task-id={task.operation_id}
                key={task.operation_id}
                onClick={() => onInspectTask(task.operation_id)}
                type="button"
              >
                <span className={`run-state-dot ${task.status}`} />
                <span className="run-history-copy">
                  <strong>
                    {taskKindLabel(task.kind)} · attempt {task.attempt}
                  </strong>
                  <span className="run-history-meta">
                    {taskStatusLabel(task)} · {formatTimestamp(task.created_at)}
                  </span>
                </span>
              </button>
            ))}
            {tasks.length === 0 && <span className="quiet-empty">No Agent tasks yet.</span>}
          </nav>

          <div className="run-inspector-detail">
            <section aria-label="Project revision summaries">
              <h4>Project revisions</h4>
              {loading ? (
                <div className="quiet-empty" role="status">
                  Loading project revisions…
                </div>
              ) : error ? (
                <div className="quiet-empty" role="alert">
                  {error}
                </div>
              ) : newestFirst.length > 0 ? (
                <ol className="run-events">
                  {newestFirst.map((summary) => (
                    <li className="info" key={summary.to_revision}>
                      <Clock3 size={12} />
                      <div>
                        <p>
                          <strong>
                            Revision {summary.from_revision} to revision {summary.to_revision}
                          </strong>
                        </p>
                        {summary.sentences.map((sentence, index) => (
                          <p key={`${summary.to_revision}:${index}`}>{sentence}</p>
                        ))}
                        <time dateTime={summary.created_at}>
                          {revisionKindLabel(summary.kind)} · {revisionAttribution(summary)} ·{" "}
                          {formatTimestamp(summary.created_at)}
                        </time>
                        {summary.producer === "agent" && summary.authorized_by && (
                          <p className="history-attribution-detail">
                            Ordinary Agent task{summary.task_id ? ` · ${summary.task_id}` : ""}
                          </p>
                        )}
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <div className="quiet-empty">No project revisions yet.</div>
              )}
            </section>
          </div>
        </div>
      </aside>
    </div>
  );
}

function revisionAttribution(summary: RevisionSummary): string {
  if (summary.producer === "system") return "RCP";
  if (summary.authorized_by) return summary.authorized_by.display_name;
  const role = summary.author ? capitalize(summary.author) : capitalize(summary.producer);
  return `${role} · Unattributed`;
}

function revisionKindLabel(kind: RevisionSummary["kind"]): string {
  if (kind === "experiment_loop") return "Experiment loop";
  return capitalize(kind);
}

function capitalize(value: string): string {
  return `${value.charAt(0).toUpperCase()}${value.slice(1)}`;
}

function formatTimestamp(value: string): string {
  const timestamp = new Date(value);
  return Number.isNaN(timestamp.getTime()) ? value : timestamp.toLocaleString();
}
