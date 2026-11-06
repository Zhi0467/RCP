// The typed client for the member's agent threads. A project thread is an
// ordinary project chat, so its transcript, sends, tasks and questions use the
// chat routes; the Space thread has its own routes with the same shapes.

import { api, ApiError } from "../core/api.ts";
import type {
  AgentCardList,
  AgentQuestion,
  AgentRunConfig,
  AgentSchedule,
  AgentScheduleList,
  AgentTask,
  AgentThread,
  AgentThreadList,
  AgentThreadMemory,
  AgentThreadQueue,
  AgentThreadTurnRequest,
  AgentThreadVoiceEntry,
  ApprovalCardDecision,
  ApprovalCardResult,
  ChatMessage,
  ChatTranscript,
  ProposalCardDecisionResult,
  RunningList,
  SteerRequest,
  WatcherRecord,
} from "../core/types";

type Request = typeof api;
type ThreadTaskOwner = Pick<AgentThread, "kind" | "project_id" | "thread_id">;

const THREADS = "/api/agent-threads";

/** The member-owned routes of one thread. */
export function threadPath(threadId: string): string {
  return `${THREADS}/${encodeURIComponent(threadId)}`;
}

export function threadProjectPath(thread: ThreadTaskOwner): string {
  if (thread.kind !== "project" || !thread.project_id)
    throw new Error("This thread has no project.");
  return `/api/projects/${encodeURIComponent(thread.project_id)}`;
}

/** Where a thread's turns are read and controlled. */
export function threadTasksPath(thread: ThreadTaskOwner): string {
  return thread.kind === "space"
    ? `${threadPath(thread.thread_id)}/tasks`
    : `${threadProjectPath(thread)}/tasks`;
}

/** Lists every thread, creating any that are missing. */
export function loadAgentThreads(request: Request = api): Promise<AgentThreadList> {
  return request(THREADS);
}

export function loadThreadTranscript(
  thread: AgentThread,
  request: Request = api,
): Promise<ChatTranscript> {
  return request(
    thread.kind === "space"
      ? `${threadPath(thread.thread_id)}/transcript`
      : `${threadProjectPath(thread)}/chats/${encodeURIComponent(thread.thread_id)}`,
  );
}

/** Starts one Work turn; the request id makes a resend safe. */
export async function sendThreadTurn(
  thread: AgentThread,
  turn: AgentThreadTurnRequest,
  requestId: string,
  request: Request = api,
): Promise<{ operation_id: string }> {
  const init = (body: object): RequestInit => ({
    method: "POST",
    body: JSON.stringify(body),
    headers: { "Idempotency-Key": requestId },
  });
  if (thread.kind === "space")
    return request(`${threadPath(thread.thread_id)}/turns`, init(turn), { retryIdentity: false });
  const task = await request<AgentTask>(
    `${threadProjectPath(thread)}/tasks/project_chat`,
    init({ chat_scope: "project", chat_id: thread.thread_id, mode: "work", ...turn }),
    { retryIdentity: false },
  );
  return { operation_id: task.operation_id };
}

/** A Space thread has no queue; its sends keep today's wait. */
export async function loadThreadQueue(
  thread: AgentThread,
  request: Request = api,
): Promise<AgentThreadQueue> {
  if (thread.kind !== "project") return { items: [] };
  return request(`${threadPath(thread.thread_id)}/queue`);
}

/** Queues a project send's message, files and references; the request id makes a resend safe. */
export function queueThreadTurn(
  thread: AgentThread,
  { message, attachment_set_id, attachment_client_id, references }: AgentThreadTurnRequest,
  requestId: string,
  request: Request = api,
): Promise<{ queue_id: string; operation_id: string | null }> {
  return request(
    `${threadPath(thread.thread_id)}/queue`,
    {
      method: "POST",
      body: JSON.stringify({ message, attachment_set_id, attachment_client_id, references }),
      headers: { "Idempotency-Key": requestId },
    },
    { retryIdentity: false },
  );
}

export function removeQueuedTurn(
  thread: AgentThread,
  queueId: string,
  request: Request = api,
): Promise<void> {
  return request(`${threadPath(thread.thread_id)}/queue/${encodeURIComponent(queueId)}`, {
    method: "DELETE",
  });
}

export function loadThreadTask(
  thread: ThreadTaskOwner,
  operationId: string,
  request: Request = api,
): Promise<AgentTask> {
  return request(`${threadTasksPath(thread)}/${encodeURIComponent(operationId)}`);
}

export function steerThreadTurn(
  thread: ThreadTaskOwner,
  operationId: string,
  steer: SteerRequest,
  request: Request = api,
): Promise<ChatMessage> {
  return request(
    `${threadTasksPath(thread)}/${encodeURIComponent(operationId)}/steer`,
    { method: "POST", body: JSON.stringify(steer) },
    { retryIdentity: false },
  );
}

/** Pause stores nothing and repeats as a no-op, so its route takes no Idempotency-Key. */
export function taskControlHeaders(
  action: "pause" | "resume" | "retry",
  requestId: string,
): Record<string, string> {
  return action === "pause" ? {} : { "Idempotency-Key": requestId };
}

export function controlThreadTask(
  thread: ThreadTaskOwner,
  operationId: string,
  action: "pause" | "resume" | "retry",
  requestId: string,
  request: Request = api,
): Promise<AgentTask> {
  return request(`${threadTasksPath(thread)}/${encodeURIComponent(operationId)}/${action}`, {
    method: "POST",
    headers: taskControlHeaders(action, requestId),
  });
}

/** A Space thread never asks plainly and its cards come with the dock's card list, so it has no question route. */
export async function loadThreadQuestions(
  thread: AgentThread,
  request: Request = api,
): Promise<AgentQuestion[]> {
  if (thread.kind === "space") return [];
  return request(
    `${threadProjectPath(thread)}/chats/${encodeURIComponent(thread.thread_id)}/questions`,
  );
}

export function renameThread(
  threadId: string,
  title: string,
  request: Request = api,
): Promise<AgentThread> {
  return request(`${threadPath(threadId)}/title`, {
    method: "POST",
    body: JSON.stringify({ title }),
  });
}

export function markThreadRead(
  threadId: string,
  readThrough: string,
  request: Request = api,
): Promise<AgentThread> {
  return request(`${threadPath(threadId)}/read`, {
    method: "POST",
    body: JSON.stringify({ read_through: readThrough }),
  });
}

/** Stores the thread's agent pick; every later turn of the thread runs it. */
export function setThreadAgent(
  threadId: string,
  agent: AgentRunConfig,
  request: Request = api,
): Promise<AgentThread> {
  // An empty field means "the provider's default", which the route expresses by omission.
  const pick = Object.fromEntries(Object.entries(agent).filter(([, value]) => value));
  return request(`${threadPath(threadId)}/agent`, {
    method: "POST",
    body: JSON.stringify(pick),
  });
}

export function setThreadBrowser(
  threadId: string,
  browserRequested: boolean,
  request: Request = api,
): Promise<{ browser_requested: boolean }> {
  return request(`${threadPath(threadId)}/browser`, {
    method: "PUT",
    body: JSON.stringify({ browser_requested: browserRequested }),
  });
}

export function loadThreadSchedules(
  threadId: string,
  request: Request = api,
): Promise<AgentScheduleList> {
  return request(`${threadPath(threadId)}/schedules`);
}

/** A project thread's live watchers; the Space thread has none. */
export async function loadThreadWatchers(
  thread: ThreadTaskOwner,
  request: Request = api,
): Promise<WatcherRecord[]> {
  if (thread.kind !== "project") return [];
  const watchers: WatcherRecord[] = await request(`${threadProjectPath(thread)}/watchers`);
  return watchers.filter((watcher) => watcher.chat_id === thread.thread_id && watcher.wakes_chat);
}

export function stopThreadWatcher(
  thread: ThreadTaskOwner,
  watcherId: string,
  request: Request = api,
): Promise<WatcherRecord> {
  return request(`${threadProjectPath(thread)}/watchers/${encodeURIComponent(watcherId)}/stop`, {
    method: "POST",
  });
}

export function cancelThreadSchedule(
  threadId: string,
  scheduleId: string,
  request: Request = api,
): Promise<void> {
  return request(`${threadPath(threadId)}/schedules/${encodeURIComponent(scheduleId)}`, {
    method: "DELETE",
  });
}

export function enableThreadSchedule(
  threadId: string,
  scheduleId: string,
  request: Request = api,
): Promise<AgentSchedule> {
  return request(`${threadPath(threadId)}/schedules/${encodeURIComponent(scheduleId)}/enable`, {
    method: "POST",
  });
}

/** Pause keeps the schedule's limits; resume skips the fires it missed. */
export function setThreadSchedulePaused(
  threadId: string,
  scheduleId: string,
  paused: boolean,
  request: Request = api,
): Promise<AgentSchedule> {
  const action = paused ? "pause" : "resume";
  return request(`${threadPath(threadId)}/schedules/${encodeURIComponent(scheduleId)}/${action}`, {
    method: "POST",
  });
}

/** Display-only voice lines; each `entry_id` is appended once. */
export function appendVoiceEntries(
  threadId: string,
  entries: AgentThreadVoiceEntry[],
  request: Request = api,
): Promise<{ appended: number }> {
  return request(`${threadPath(threadId)}/voice-entries`, {
    method: "POST",
    body: JSON.stringify({ entries }),
  });
}

export function loadThreadMemory(
  threadId: string,
  request: Request = api,
): Promise<AgentThreadMemory> {
  return request(`${threadPath(threadId)}/memory`);
}

/** Only the member's own tap reaches this; no page or voice tool calls it. */
export function resolveApprovalCard(
  questionId: string,
  decision: ApprovalCardDecision,
  request: Request = api,
): Promise<ApprovalCardResult> {
  return request(`/api/approval-cards/${encodeURIComponent(questionId)}`, {
    method: "POST",
    body: JSON.stringify(decision),
  });
}

/** The member's open cards: one project thread's, or every thread's for Pip's dock. */
export function loadAgentCards(threadId?: string, request: Request = api): Promise<AgentCardList> {
  return request(`${THREADS}/cards${threadId ? `?thread_id=${encodeURIComponent(threadId)}` : ""}`);
}

/** A failed read hides the cards and nothing else. */
export function readAgentCards(
  threadId?: string,
  request: Request = api,
): Promise<AgentCardList | null> {
  return loadAgentCards(threadId, request).catch(() => null);
}

/** The member's runs and project agents across their projects, for the Space thread. */
export function loadRunningList(request: Request = api): Promise<RunningList> {
  return request(`${THREADS}/running`);
}

export type ProposalCardOutcome =
  | { outcome: "decided"; status: ProposalCardDecisionResult["status"] }
  | { outcome: "gone" }
  | { outcome: "retry" }
  | { outcome: "refused"; reason: string };

/**
 * Decides a Proposal from its card, as the member. A Proposal no longer pending
 * redraws from a refetch, a moved graph or an unreachable backend asks for another
 * tap, and any other refusal shows the server's reason.
 */
export async function decideProposalCard(
  projectId: string,
  proposalId: string,
  decision: "approved" | "rejected",
  request: Request = api,
): Promise<ProposalCardOutcome> {
  try {
    const result = await request<ProposalCardDecisionResult>(
      `/api/approval-cards/proposals/${encodeURIComponent(projectId)}/${encodeURIComponent(proposalId)}`,
      { method: "POST", body: JSON.stringify({ decision }) },
    );
    return { outcome: "decided", status: result.status };
  } catch (failure) {
    if (
      !(failure instanceof ApiError) ||
      failure.status === 503 ||
      failure.code === "graph_changed"
    )
      return { outcome: "retry" };
    if (failure.code === "proposal_not_pending") return { outcome: "gone" };
    return { outcome: "refused", reason: failure.message };
  }
}
