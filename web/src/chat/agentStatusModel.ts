import type {
  AgentCompaction,
  AgentStatus,
  AgentSubagent,
  AgentTask,
  AgentTaskRequest,
  ChatMessage,
} from "../core/types";
import type { TaskTranscriptLine } from "../agents/agentTasks";

/** The task fields the status strip reads; a task or an older turn known only from history. */
export type StatusTask = Pick<
  AgentTask,
  | "operation_id"
  | "active"
  | "finished"
  | "native_session_id"
  | "runtime_id"
  | "runtime_label"
  | "agent_status"
  | "parent_operation_id"
> & { request: Pick<AgentTaskRequest, "provider" | "model" | "session_id"> };

/** The chat's tasks, preceded by earlier turns that fell outside the bounded task
 *  list and are known only from the transcript the chat route joined status into.
 *  A turn without status stays, so a later status-less turn is still the latest. */
export function chatStatusTasks(tasks: AgentTask[], messages: ChatMessage[]): StatusTask[] {
  const known = new Set(tasks.map((task) => task.operation_id));
  const history: StatusTask[] = [];
  for (const message of messages) {
    const operationId = message.operation_id;
    if (message.role !== "assistant" || !operationId || known.has(operationId)) continue;
    known.add(operationId);
    history.push({
      operation_id: operationId,
      active: false,
      finished: true,
      native_session_id: message.native_session_id,
      runtime_id: "",
      runtime_label: "",
      parent_operation_id: null,
      agent_status: message.agent_status ?? null,
      request: {
        provider: (message.provider as AgentTaskRequest["provider"]) ?? null,
        // The transcript names an unset model "provider-default"; a task leaves it null.
        model: message.model === "provider-default" ? null : message.model,
      },
    });
  }
  return [...history, ...tasks];
}

export interface ChatCompaction extends AgentCompaction {
  operationId: string;
  /** 1-based position of the turn among the chat's tasks. */
  turn: number;
}

export interface AgentStatusView {
  task: StatusTask;
  contextTokens: number | null;
  contextWindow: number | null;
  autocompactTokens: number | null;
  compacting: boolean;
  subagents: AgentSubagent[];
  runningSubagents: number;
  compactions: ChatCompaction[];
}

/** The chat's current turn (running, queued or paused), else its latest finished turn. */
export function agentStatusTask(tasks: StatusTask[]): StatusTask | null {
  // A retry is a child attempt; its paused parent is no longer the current turn.
  const retried = new Set(tasks.map((task) => task.parent_operation_id).filter(Boolean));
  const current = tasks.filter((task) => !retried.has(task.operation_id));
  return (
    lastWhere(current, (task) => !task.finished) ?? lastWhere(current, (task) => task.finished)
  );
}

/** The task's own window, else the newest one an earlier task of the chat
 *  learned for the same provider and model (Claude reports it at turn end). */
export function agentContextWindow(task: StatusTask, tasks: StatusTask[]): number | null {
  const own = task.agent_status?.context_window;
  if (own) return own;
  const match = lastEarlier(
    task,
    tasks,
    (candidate) =>
      Boolean(candidate.agent_status?.context_window) &&
      (candidate.request.provider ?? null) === (task.request.provider ?? null) &&
      (candidate.request.model ?? null) === (task.request.model ?? null),
  );
  return match?.agent_status?.context_window ?? null;
}

/** The task's own context, else, until an unfinished resumed turn reports one,
 *  the context its native session ended the previous turn with on the same
 *  runtime (an exec fallback never reports context, so it inherits none). */
export function agentContextTokens(task: StatusTask, tasks: StatusTask[]): number | null {
  const own = task.agent_status?.context_tokens;
  if (own != null) return own;
  const session = task.request.session_id;
  if (!session || task.finished) return null;
  const match = lastEarlier(
    task,
    tasks,
    (candidate) =>
      candidate.native_session_id === session &&
      candidate.runtime_id === task.runtime_id &&
      candidate.agent_status?.context_tokens != null,
  );
  return match?.agent_status?.context_tokens ?? null;
}

function lastEarlier(
  task: StatusTask,
  tasks: StatusTask[],
  predicate: (candidate: StatusTask) => boolean,
): StatusTask | null {
  const index = tasks.indexOf(task);
  return lastWhere(index < 0 ? tasks : tasks.slice(0, index), predicate);
}

function lastWhere<T>(items: T[], predicate: (item: T) => boolean): T | null {
  for (let index = items.length - 1; index >= 0; index -= 1)
    if (predicate(items[index])) return items[index];
  return null;
}

export function chatCompactions(tasks: StatusTask[]): ChatCompaction[] {
  return tasks.flatMap((task, index) =>
    (task.agent_status?.compactions ?? []).map((compaction) => ({
      ...compaction,
      operationId: task.operation_id,
      turn: index + 1,
    })),
  );
}

export function runningSubagentCount(status: AgentStatus | null | undefined): number {
  return (status?.subagents ?? []).filter((subagent) => subagent.state === "running").length;
}

export function agentStatusView(tasks: StatusTask[]): AgentStatusView | null {
  const task = agentStatusTask(tasks);
  if (!task) return null;
  const status = task.agent_status;
  return {
    task,
    contextTokens: agentContextTokens(task, tasks),
    contextWindow: agentContextWindow(task, tasks),
    autocompactTokens: status?.autocompact_tokens ?? null,
    compacting: Boolean(status?.compacting),
    subagents: status?.subagents ?? [],
    runningSubagents: runningSubagentCount(status),
    compactions: chatCompactions(tasks),
  };
}

/** Fraction of the window in use, clamped to [0, 1]; null when either side is unknown. */
export function contextFraction(view: AgentStatusView): number | null {
  if (view.contextTokens == null || !view.contextWindow) return null;
  return Math.min(1, Math.max(0, view.contextTokens / view.contextWindow));
}

/** 119k, 1.2M, 850. */
export function formatTokens(value: number): string {
  if (value < 1000) return String(Math.round(value));
  if (value < 1_000_000) return `${Math.round(value / 1000)}k`;
  const millions = value / 1_000_000;
  return `${Number.isInteger(millions) ? millions : millions.toFixed(1)}M`;
}

export interface CompactionAnchor {
  placement: "before" | "after";
  compactions: AgentCompaction[];
}

/**
 * Where each compacted turn's divider goes: before the turn's first agent-side
 * line, or after its last line when the turn has only the human line so far.
 */
export function compactionAnchors(
  lines: TaskTranscriptLine[],
  tasks: StatusTask[],
): Map<string, CompactionAnchor> {
  const anchors = new Map<string, CompactionAnchor>();
  for (const task of tasks) {
    const compactions = task.agent_status?.compactions ?? [];
    if (!compactions.length) continue;
    const own = lines.filter((line) => line.taskId === task.operation_id);
    const reply = own.find((line) => line.role !== "human");
    if (reply) anchors.set(reply.lineId, { placement: "before", compactions });
    else if (own.length) anchors.set(own.at(-1)!.lineId, { placement: "after", compactions });
  }
  return anchors;
}
