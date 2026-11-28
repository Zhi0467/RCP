import type { AgentTask, ChatMessage } from "../core/types";
import { agentStatusView, type StatusTask } from "./agentStatusModel.ts";

/** Below this, a cold re-read of the session costs too little to be worth a summary. */
export const COMPACT_OFFER_MIN_TOKENS = 200_000;
/** Provider prompt caches have expired by then, so the next turn re-reads everything. */
export const COMPACT_OFFER_IDLE_MS = 60 * 60 * 1000;

export interface CompactOffer {
  contextTokens: number;
  idleMs: number;
}

/**
 * Whether a human's next message should first be offered a compaction: its
 * resumed session is large, has been idle past the provider caches, and runs
 * on a runtime that can compact it. Any turn, watcher or scheduled, resets the
 * idle clock, so a message sent after the offer is never offered again.
 */
export function compactOffer({
  tasks,
  statusTasks,
  messages,
  sessionId,
  now,
}: {
  tasks: readonly AgentTask[];
  statusTasks: StatusTask[];
  messages: readonly ChatMessage[];
  sessionId: string | null | undefined;
  now: number;
}): CompactOffer | null {
  if (!sessionId || tasks.some((task) => !task.finished)) return null;
  const holder = [...tasks].reverse().find((task) => task.native_session_id === sessionId);
  if (!holder?.can_compact_first) return null;
  const contextTokens = agentStatusView(statusTasks)?.contextTokens;
  if (contextTokens == null || contextTokens < COMPACT_OFFER_MIN_TOKENS) return null;
  const times = [
    ...tasks.map((task) => task.finished_at ?? task.updated_at),
    ...messages.map((message) => message.timestamp),
  ]
    .map((time) => (time ? Date.parse(time) : Number.NaN))
    .filter((time) => Number.isFinite(time));
  if (!times.length) return null;
  const idleMs = now - Math.max(...times);
  return idleMs >= COMPACT_OFFER_IDLE_MS ? { contextTokens, idleMs } : null;
}

/** A compact-first turn that failed before its compaction finished never sent its message.
 *  Only a first attempt compacts; a Resume or Retry already sent it. */
export function compactionUnsent(task: AgentTask): boolean {
  return Boolean(
    task.request.compact_first &&
    !task.parent_operation_id &&
    task.failed &&
    !(task.agent_status?.compactions ?? []).some((compaction) => compaction.trigger === "manual"),
  );
}

/** 45 min, 3 h, 2 d. */
export function formatIdle(ms: number): string {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} d`;
}
