import {
  AlertTriangle,
  Check,
  CheckCircle2,
  CirclePause,
  Clock3,
  Copy,
  LoaderCircle,
  Play,
  RotateCcw,
  X,
} from "lucide-react";
import { useState } from "react";
import { isActiveTask, reconstructTaskTranscript, taskKindLabel } from "../agentTasks";
import type { AgentTask, AgentTaskReceipt } from "../types";
import { estimateLabel, formatDuration, taskStatusLabel } from "./AgentTaskActivity";

interface Props {
  tasks: AgentTask[];
  task: AgentTask | null;
  loading: boolean;
  actionBusy: boolean;
  mutatingActionsDisabled?: boolean;
  onSelect: (taskId: string) => void;
  onPause: () => void;
  onResume: () => void;
  onRetry: () => void;
  onDismiss: () => void;
  onClose: () => void;
}

export function AgentTaskInspector({
  tasks,
  task,
  loading,
  actionBusy,
  mutatingActionsDisabled = false,
  onSelect,
  onPause,
  onResume,
  onRetry,
  onDismiss,
  onClose,
}: Props) {
  const [copiedReceiptId, setCopiedReceiptId] = useState<number | null>(null);
  const transcript = task ? reconstructTaskTranscript([task]) : [];
  const promptReceipts = task?.debug_receipts?.filter(isPromptReceipt) ?? [];

  async function copyPrompt(receipt: AgentTaskReceipt) {
    const prompt = receipt.payload.prompt;
    if (typeof prompt !== "string") return;
    try {
      await navigator.clipboard.writeText(prompt);
    } catch {
      return;
    }
    setCopiedReceiptId(receipt.receipt_id);
    window.setTimeout(() => setCopiedReceiptId(null), 1600);
  }

  return (
    <div
      className="drawer-scrim"
      role="presentation"
      onMouseDown={(event) => {
        if (event.currentTarget === event.target) onClose();
      }}
    >
      <aside className="detail-drawer run-inspector" aria-label="Agent task inspector">
        <header>
          <h2>Agent tasks</h2>
          <button className="icon-button" aria-label="Close agent task inspector" onClick={onClose}>
            <X size={16} />
          </button>
        </header>

        <div className="run-inspector-body">
          <nav className="run-history" aria-label="Agent task history">
            {tasks.map((item) => (
              <button
                key={item.operation_id}
                className={task?.operation_id === item.operation_id ? "active" : ""}
                onClick={() => onSelect(item.operation_id)}
              >
                <span className={`run-state-dot ${item.status}`} />
                <span className="run-history-copy">
                  <strong>
                    {taskKindLabel(item.kind)} · attempt {item.attempt}
                  </strong>
                  <span className="run-history-meta">
                    {taskStatusLabel(item)} · {formatTimestamp(item.created_at)}
                  </span>
                </span>
              </button>
            ))}
          </nav>

          <div className="run-inspector-detail">
            {loading && !task ? (
              <div className="quiet-empty">
                <LoaderCircle className="spin" size={15} />
                <span>Loading task</span>
              </div>
            ) : task ? (
              <>
                <section className="run-detail-hero">
                  <div className={`run-detail-icon ${task.status}`}>
                    {task.status === "succeeded" ? (
                      <CheckCircle2 size={20} />
                    ) : task.status === "paused" ? (
                      <CirclePause size={20} />
                    ) : isActiveTask(task) ? (
                      <LoaderCircle className="spin" size={20} />
                    ) : (
                      <AlertTriangle size={20} />
                    )}
                  </div>
                  <div>
                    <span className="eyebrow">
                      {taskKindLabel(task.kind)} · attempt {task.attempt}
                    </span>
                    <h3>{taskStatusLabel(task)}</h3>
                    <p>{task.error || task.status_message}</p>
                  </div>
                </section>

                <section className="run-detail-progress">
                  <div>
                    <span>Estimated progress</span>
                    <strong>{Math.round(task.progress * 100)}%</strong>
                  </div>
                  <div className="run-progress-track large">
                    <span style={{ width: `${Math.round(task.progress * 100)}%` }} />
                  </div>
                  <span className="run-progress-meta">
                    {estimateLabel(task)} · {formatDuration(task.elapsed_seconds)} elapsed
                  </span>
                </section>

                <section>
                  <h4>Task contract</h4>
                  <dl className="detail-list run-contract-list">
                    <div>
                      <dt>Kind</dt>
                      <dd>{taskKindLabel(task.kind)}</dd>
                    </div>
                    <div>
                      <dt>Provider</dt>
                      <dd>
                        {task.request.provider || "Project default"}
                        {task.request.model ? ` · ${task.request.model}` : ""}
                      </dd>
                    </div>
                    <div>
                      <dt>Execution</dt>
                      <dd>{task.request.run_on || "Project default"}</dd>
                    </div>
                    {task.request.run_truth_scope && (
                      <div>
                        <dt>Truth scope</dt>
                        <dd>{task.request.run_truth_scope.join(", ")}</dd>
                      </div>
                    )}
                    {task.request.node_id && (
                      <div>
                        <dt>Node</dt>
                        <dd className="mono">{task.request.node_id}</dd>
                      </div>
                    )}
                    {task.native_session_id && (
                      <div>
                        <dt>Native session</dt>
                        <dd className="mono">{shortId(task.native_session_id)}</dd>
                      </div>
                    )}
                    <div>
                      <dt>Task</dt>
                      <dd className="mono">{task.operation_id}</dd>
                    </div>
                    {task.parent_operation_id && (
                      <div>
                        <dt>Continues</dt>
                        <dd className="mono">{task.parent_operation_id}</dd>
                      </div>
                    )}
                    <div>
                      <dt>Started</dt>
                      <dd>{formatTimestamp(task.started_at || task.created_at)}</dd>
                    </div>
                  </dl>
                </section>

                {(transcript.length > 0 || task.result) && (
                  <section>
                    <h4>Result</h4>
                    <div className="task-result-transcript">
                      {transcript.map((line, index) => (
                        <div
                          className={`node-chat-line ${line.role}`}
                          key={`${line.taskId}-${index}`}
                        >
                          {line.text}
                        </div>
                      ))}
                      {task.result && !transcript.length && (
                        <pre>{JSON.stringify(task.result, null, 2)}</pre>
                      )}
                    </div>
                  </section>
                )}

                {promptReceipts.length > 0 && (
                  <section>
                    <h4>Exact prompts</h4>
                    <div className="run-prompt-list">
                      {promptReceipts.map((receipt, index) => {
                        const prompt = receipt.payload.prompt as string;
                        const metadata = promptMetadata(receipt);
                        return (
                          <details className="run-prompt" key={receipt.receipt_id}>
                            <summary>
                              <strong>{promptLabel(receipt, index)}</strong>
                              <time>{formatTimestamp(receipt.created_at)}</time>
                            </summary>
                            <div className="run-prompt-body">
                              {metadata.length > 0 && (
                                <dl className="run-prompt-metadata">
                                  {metadata.map(([label, value]) => (
                                    <div key={label}>
                                      <dt>{label}</dt>
                                      <dd>{value}</dd>
                                    </div>
                                  ))}
                                </dl>
                              )}
                              <div className="run-prompt-toolbar">
                                <span>{promptStats(receipt, prompt)}</span>
                                <button
                                  className="button ghost compact"
                                  type="button"
                                  onClick={() => void copyPrompt(receipt)}
                                >
                                  {copiedReceiptId === receipt.receipt_id ? (
                                    <Check size={12} />
                                  ) : (
                                    <Copy size={12} />
                                  )}
                                  {copiedReceiptId === receipt.receipt_id ? "Copied" : "Copy"}
                                </button>
                              </div>
                              <pre>{prompt}</pre>
                            </div>
                          </details>
                        );
                      })}
                    </div>
                  </section>
                )}

                <section>
                  <h4>Event trail</h4>
                  <ol className="run-events">
                    {(task.events || []).map((event) => (
                      <li key={event.event_id} className={event.level}>
                        <Clock3 size={12} />
                        <div>
                          <p>{event.message}</p>
                          <time>{formatTimestamp(event.created_at)}</time>
                        </div>
                      </li>
                    ))}
                    {!task.events?.length && <li className="empty">No persisted events yet.</li>}
                  </ol>
                </section>
              </>
            ) : (
              <div className="quiet-empty">No task selected.</div>
            )}
          </div>
        </div>

        {task &&
          (task.can_pause ||
            task.can_resume ||
            task.can_retry ||
            task.status === "failed" ||
            task.status === "interrupted" ||
            task.status === "paused") && (
            <footer className="drawer-actions run-inspector-actions">
              <div>
                {task.can_pause && (
                  <button className="button secondary" disabled={actionBusy} onClick={onPause}>
                    <CirclePause size={14} /> Pause
                  </button>
                )}
                {task.can_retry && (
                  <button
                    className="button secondary"
                    disabled={actionBusy || mutatingActionsDisabled}
                    onClick={onRetry}
                  >
                    <RotateCcw size={14} />{" "}
                    {task.kind === "seed" || task.kind === "refresh" ? "Retry…" : "Retry"}
                  </button>
                )}
                {task.can_resume && (
                  <button
                    className="button primary"
                    disabled={actionBusy || mutatingActionsDisabled}
                    onClick={onResume}
                  >
                    <Play size={14} /> Resume
                  </button>
                )}
                {(task.status === "failed" ||
                  task.status === "interrupted" ||
                  task.status === "paused") && (
                  <button className="button ghost" type="button" onClick={onDismiss}>
                    <X size={14} /> Dismiss notification
                  </button>
                )}
              </div>
            </footer>
          )}
      </aside>
    </div>
  );
}

function isPromptReceipt(receipt: AgentTaskReceipt): boolean {
  return receipt.category === "agent_prompt" && typeof receipt.payload.prompt === "string";
}

function promptLabel(receipt: AgentTaskReceipt, index: number): string {
  const payload = receipt.payload;
  const continuation = firstString(payload, "continuation_cause", "continuation");
  if (continuation) return `${continuationLabel(continuation)} · ${index + 1}`;
  const explicit = firstString(
    payload,
    "label",
    "launch_kind",
    "prompt_kind",
    "launch_type",
    "mode",
  );
  return explicit ? `${humanize(explicit)} · ${index + 1}` : `Provider launch ${index + 1}`;
}

function promptMetadata(receipt: AgentTaskReceipt): [string, string][] {
  const payload = receipt.payload;
  const fields = [
    "provider",
    "model",
    "reasoning",
    "run_on",
    "attempt",
    "continuation_cause",
    "correction_round",
    "native_session_id",
  ];
  return fields.flatMap((field) => {
    const value = payload[field];
    if (typeof value !== "string" && typeof value !== "number" && typeof value !== "boolean")
      return [];
    return [[humanize(field), String(value)] as [string, string]];
  });
}

function continuationLabel(value: string): string {
  if (value === "fresh") return "First attempt";
  if (value === "resume") return "Continuing after interruption";
  if (value === "correction") return "Correcting prior failure";
  if (value === "handoff") return "Continuing in a new session";
  return humanize(value);
}

function promptStats(receipt: AgentTaskReceipt, prompt: string): string {
  const lineCount =
    typeof receipt.payload.line_count === "number"
      ? receipt.payload.line_count
      : prompt.split("\n").length;
  const sha =
    typeof receipt.payload.sha256 === "string" ? ` · SHA-256 ${receipt.payload.sha256}` : "";
  return `${lineCount.toLocaleString()} lines${sha}`;
}

function firstString(payload: Record<string, unknown>, ...fields: string[]): string | null {
  for (const field of fields) {
    const value = payload[field];
    if (typeof value === "string" && value.trim()) return value;
  }
  return null;
}

function humanize(value: string): string {
  const words = value.replaceAll("_", " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function shortId(value: string): string {
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function formatTimestamp(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString([], {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}
