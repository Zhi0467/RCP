import type { AgentCompaction, AgentStatus, AgentSubagent, AgentTask } from "../core/types";
import type { TaskTranscriptLine } from "../agents/agentTasks";

export interface ChatCompaction extends AgentCompaction {
  operationId: string;
  /** 1-based position of the turn among the chat's tasks. */
  turn: number;
}

export interface AgentStatusView {
  task: AgentTask;
  contextTokens: number | null;
  contextWindow: number | null;
  autocompactTokens: number | null;
  compacting: boolean;
  subagents: AgentSubagent[];
  runningSubagents: number;
  compactions: ChatCompaction[];
}

/** The chat's current turn when one is active, else its latest finished turn. */
export function agentStatusTask(tasks: AgentTask[]): AgentTask | null {
  return lastWhere(tasks, (task) => task.active) ?? lastWhere(tasks, (task) => task.finished);
}

/** The task's own window, else the newest one an earlier task of the chat
 *  learned for the same provider and model (Claude reports it at turn end). */
export function agentContextWindow(task: AgentTask, tasks: AgentTask[]): number | null {
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

/** The task's own context, else, until a resumed turn reports one, the context
 *  its native session ended the previous turn with. */
export function agentContextTokens(task: AgentTask, tasks: AgentTask[]): number | null {
  const own = task.agent_status?.context_tokens;
  if (own != null) return own;
  const session = task.request.session_id;
  if (!session) return null;
  const match = lastEarlier(
    task,
    tasks,
    (candidate) =>
      candidate.native_session_id === session && candidate.agent_status?.context_tokens != null,
  );
  return match?.agent_status?.context_tokens ?? null;
}

function lastEarlier(
  task: AgentTask,
  tasks: AgentTask[],
  predicate: (candidate: AgentTask) => boolean,
): AgentTask | null {
  const index = tasks.indexOf(task);
  return lastWhere(index < 0 ? tasks : tasks.slice(0, index), predicate);
}

function lastWhere<T>(items: T[], predicate: (item: T) => boolean): T | null {
  for (let index = items.length - 1; index >= 0; index -= 1)
    if (predicate(items[index])) return items[index];
  return null;
}

export function chatCompactions(tasks: AgentTask[]): ChatCompaction[] {
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

export function agentStatusView(tasks: AgentTask[]): AgentStatusView | null {
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
  tasks: AgentTask[],
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
