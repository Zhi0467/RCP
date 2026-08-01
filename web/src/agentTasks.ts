import type {
  AgentArtifactDescriptor,
  AgentTask,
  AgentTaskKind,
  ChatMessage,
  ConversationMode,
  GraphUpdateResult,
} from "./types";

export interface TaskTranscriptLine {
  role: "human" | "agent" | "error" | "meta";
  text: string;
  taskId: string;
  artifacts?: AgentArtifactDescriptor[];
  mode?: ConversationMode | null;
  graphUpdate?: GraphUpdateResult | null;
}

export function isActiveTask(task: AgentTask): boolean {
  return task.status === "queued" || task.status === "running" || task.status === "pausing";
}

export function projectActivityTask(
  tasks: AgentTask[],
  observedTaskId: string | null,
): AgentTask | null {
  const active = tasks.find(isActiveTask);
  if (active) return active;
  const continuedTaskIds = new Set(
    tasks.flatMap((task) => (task.parent_operation_id ? [task.parent_operation_id] : [])),
  );
  const paused = tasks.find(
    (task) => task.status === "paused" && !continuedTaskIds.has(task.operation_id),
  );
  if (paused) return paused;
  if (!observedTaskId) return null;
  return tasks.find((task) => task.operation_id === observedTaskId) ?? null;
}

export function taskKindLabel(kind: AgentTaskKind): string {
  switch (kind) {
    case "seed":
      return "Seed project graph";
    case "refresh":
      return "Refresh project graph";
    case "node_chat":
      return "Node conversation";
    case "project_chat":
      return "Project conversation";
    case "paper_coach":
      return "Writing coach";
  }
}

export function relatedChatTasks(
  tasks: AgentTask[],
  kind: "node_chat" | "project_chat",
  nodeId?: string | null,
  requestedChatId?: string | null,
): AgentTask[] {
  const candidates = tasks
    .filter(
      (task) => task.kind === kind && (kind === "project_chat" || task.request.node_id === nodeId),
    )
    .sort(compareTaskTime);
  if (requestedChatId) return candidates.filter((task) => task.request.chat_id === requestedChatId);
  const latest = candidates.at(-1);
  if (!latest) return [];
  const chatId = textValue(latest.request.chat_id);
  return chatId ? candidates.filter((task) => task.request.chat_id === chatId) : [latest];
}

export function relatedCoachTasks(tasks: AgentTask[], sessionId: string | null): AgentTask[] {
  const candidates = tasks.filter((task) => task.kind === "paper_coach").sort(compareTaskTime);
  if (sessionId) {
    return candidates.filter(
      (task) => task.native_session_id === sessionId || task.request.session_id === sessionId,
    );
  }
  return [];
}

export function resumablePausedChatTask(tasks: AgentTask[]): AgentTask | null {
  return [...tasks].reverse().find((task) => task.status === "paused" && task.can_resume) ?? null;
}

export function chatTasksMissingFromHistory(
  tasks: AgentTask[],
  messages: ChatMessage[],
): AgentTask[] {
  const persistedOperationIds = new Set(
    messages.flatMap((message) => (message.operation_id ? [message.operation_id] : [])),
  );
  const availablePrompts = new Map<string, number[]>();
  messages.forEach((message) => {
    if (message.role !== "user") return;
    const timestamps = availablePrompts.get(message.text) ?? [];
    timestamps.push(Date.parse(message.timestamp));
    availablePrompts.set(message.text, timestamps);
  });
  availablePrompts.forEach((timestamps) => timestamps.sort((left, right) => left - right));
  return tasks.filter((task) => {
    if (persistedOperationIds.has(task.operation_id)) return false;
    const prompt = textValue(task.request.message);
    if (!prompt) return true;
    const timestamps = availablePrompts.get(prompt) ?? [];
    const taskCreatedAt = Date.parse(task.created_at);
    const matchIndex = timestamps.findIndex((timestamp) => timestamp >= taskCreatedAt);
    if (matchIndex < 0) return true;
    timestamps.splice(matchIndex, 1);
    return false;
  });
}

export function chatMessageTranscriptLine(message: ChatMessage): TaskTranscriptLine {
  return {
    role: message.role === "user" ? "human" : "agent",
    text: message.text,
    taskId: message.operation_id ?? message.message_id,
    mode: message.mode,
    graphUpdate: message.graph_update,
  };
}

export function reconstructTaskTranscript(tasks: AgentTask[]): TaskTranscriptLine[] {
  return [...tasks].sort(compareTaskTime).flatMap((task) => {
    const lines: TaskTranscriptLine[] = [];
    const message = textValue(task.request.message);
    const mode = conversationMode(task.request.mode);
    const graphUpdate = task.result?.graph_update ?? null;
    if (message) lines.push({ role: "human", text: message, taskId: task.operation_id, mode });
    const messages = Array.isArray(task.result?.messages)
      ? task.result.messages.filter(
          (item): item is string => typeof item === "string" && item.trim().length > 0,
        )
      : [];
    const artifacts = taskArtifacts(task);
    messages.forEach((text, index) =>
      lines.push({
        role: "agent",
        text,
        taskId: task.operation_id,
        mode,
        ...(index === messages.length - 1 && artifacts.length ? { artifacts } : {}),
        ...(index === messages.length - 1 && graphUpdate ? { graphUpdate } : {}),
      }),
    );
    if (!messages.length && (artifacts.length || (graphUpdate && graphUpdate.status !== "none"))) {
      lines.push({
        role: "agent",
        text: "",
        taskId: task.operation_id,
        mode,
        ...(artifacts.length ? { artifacts } : {}),
        ...(graphUpdate ? { graphUpdate } : {}),
      });
    }
    const graphOnlyRejection = task.status === "succeeded" && graphUpdate?.status === "rejected";
    if (task.error && !graphOnlyRejection) {
      lines.push({ role: "error", text: task.error, taskId: task.operation_id });
    } else if (
      task.status === "failed" ||
      task.status === "interrupted" ||
      task.status === "paused"
    ) {
      lines.push({
        role: task.status === "failed" ? "error" : "meta",
        text: task.status_message,
        taskId: task.operation_id,
      });
    }
    return lines;
  });
}

export function artifactUrl(
  projectId: string,
  taskId: string,
  artifactId: string,
  action: "preview" | "download",
): string {
  return `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}/${action}`;
}

export function latestNativeSessionId(tasks: AgentTask[]): string | null {
  return (
    [...tasks]
      .sort(compareTaskTime)
      .reverse()
      .find((task) => task.native_session_id)?.native_session_id ?? null
  );
}

function compareTaskTime(left: AgentTask, right: AgentTask): number {
  return (
    Date.parse(left.created_at) - Date.parse(right.created_at) ||
    left.operation_id.localeCompare(right.operation_id)
  );
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function conversationMode(value: unknown): ConversationMode | null {
  return value === "discuss" || value === "work" ? value : null;
}

function taskArtifacts(task: AgentTask): AgentArtifactDescriptor[] {
  if (!Array.isArray(task.result?.artifacts)) return [];
  return task.result.artifacts.filter(
    (item): item is AgentArtifactDescriptor =>
      typeof item === "object" &&
      item !== null &&
      typeof item.artifact_id === "string" &&
      typeof item.name === "string" &&
      typeof item.media_type === "string",
  );
}
