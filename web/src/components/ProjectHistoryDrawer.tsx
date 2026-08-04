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
            <section aria-label="Graph revision summaries">
              <h4>Graph revisions</h4>
              {loading ? (
                <div className="quiet-empty" role="status">
                  Loading graph revisions…
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
                          {revisionKindLabel(summary.kind)} · {capitalize(summary.author)} ·{" "}
                          {formatTimestamp(summary.created_at)}
                        </time>
                      </div>
                    </li>
                  ))}
                </ol>
              ) : (
                <div className="quiet-empty">No graph revisions yet.</div>
              )}
            </section>
          </div>
        </div>
      </aside>
    </div>
  );
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
