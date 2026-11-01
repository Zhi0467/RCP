// The typed client for the member's agent threads. A project thread is an
// ordinary project chat, so its transcript, sends, tasks and questions use the
// chat routes; the Space thread has its own routes with the same shapes.

import { api } from "../core/api.ts";
import type {
  AgentQuestion,
  AgentSchedule,
  AgentScheduleList,
  AgentTask,
  AgentThread,
  AgentThreadList,
  AgentThreadMemory,
  AgentThreadTurnRequest,
  AgentThreadVoiceEntry,
  ApprovalCardDecision,
  ApprovalCardResult,
  ChatMessage,
  ChatTranscript,
  SteerRequest,
} from "../core/types";

type Request = typeof api;

const THREADS = "/api/agent-threads";

function threadPath(threadId: string): string {
  return `${THREADS}/${encodeURIComponent(threadId)}`;
}

function projectPath(thread: AgentThread): string {
  if (thread.kind !== "project" || !thread.project_id)
    throw new Error("This thread has no project.");
  return `/api/projects/${encodeURIComponent(thread.project_id)}`;
}

/** Where a thread's turns are read and controlled. */
export function threadTasksPath(thread: AgentThread): string {
  return thread.kind === "space"
    ? `${threadPath(thread.thread_id)}/tasks`
    : `${projectPath(thread)}/tasks`;
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
      : `${projectPath(thread)}/chats/${encodeURIComponent(thread.thread_id)}`,
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
    `${projectPath(thread)}/tasks/project_chat`,
    init({ chat_scope: "project", chat_id: thread.thread_id, mode: "work", ...turn }),
    { retryIdentity: false },
  );
  return { operation_id: task.operation_id };
}

export function loadThreadTask(
  thread: AgentThread,
  operationId: string,
  request: Request = api,
): Promise<AgentTask> {
  return request(`${threadTasksPath(thread)}/${encodeURIComponent(operationId)}`);
}

export function steerThreadTurn(
  thread: AgentThread,
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

export function controlThreadTask(
  thread: AgentThread,
  operationId: string,
  action: "pause" | "resume" | "retry",
  requestId: string,
  request: Request = api,
): Promise<AgentTask> {
  return request(`${threadTasksPath(thread)}/${encodeURIComponent(operationId)}/${action}`, {
    method: "POST",
    headers: { "Idempotency-Key": requestId },
  });
}

/** A Space thread never asks plainly and has no cards, so it has no question route. */
export async function loadThreadQuestions(
  thread: AgentThread,
  request: Request = api,
): Promise<AgentQuestion[]> {
  if (thread.kind === "space") return [];
  return request(`${projectPath(thread)}/chats/${encodeURIComponent(thread.thread_id)}/questions`);
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
