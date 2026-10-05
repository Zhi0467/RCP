import type {
  AgentArtifactDescriptor,
  ArtifactOmissions,
  AgentTask,
  AgentTaskKind,
  ChatMessage,
  BrowserTurnStatus,
  ChatAttachmentDescriptor,
  ConversationMode,
  GraphUpdateResult,
  TaskTrigger,
  SteerReceipt,
} from "../core/types";

export interface TaskTranscriptLine {
  browserStatus?: BrowserTurnStatus | null;
  lineId: string;
  role: "human" | "agent" | "error" | "meta";
  text: string;
  taskId: string;
  timestamp: string;
  artifacts?: AgentArtifactDescriptor[];
  artifactOmissions?: ArtifactOmissions;
  attachments?: ChatAttachmentDescriptor[];
  mode?: ConversationMode | null;
  trigger?: TaskTrigger;
  graphUpdate?: GraphUpdateResult | null;
  steering?: SteerReceipt | null;
  /** Holds the live progress of a running turn the human did not type. */
  running?: boolean;
}

export function isActiveTask(task: AgentTask): boolean {
  return task.active;
}

export function isTaskNotificationSuperseded(task: AgentTask, tasks: AgentTask[]): boolean {
  // A paused turn is the one awaiting-human state the human can still resume, and
  // its notification is the way back to it, so a later success never retires it.
  if ((task.kind !== "seed" && task.kind !== "refresh") || !task.awaiting_human || task.paused)
    return false;
  return tasks.some(
    (candidate) =>
      (candidate.kind === "seed" || candidate.kind === "refresh") &&
      candidate.settled &&
      compareTaskTime(candidate, task) > 0,
  );
}

/** Style follows the answers the projection published, never a status of its own. */
export function agentTaskTone(task: AgentTask): "running" | "failed" | "paused" | "succeeded" {
  if (task.failed) return "failed";
  if (task.awaiting_human) return "paused";
  if (task.active) return "running";
  return "succeeded";
}

export function taskStatusLabel(task: AgentTask): string {
  return task.status_label;
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
  const paused = tasks.find((task) => task.paused && !continuedTaskIds.has(task.operation_id));
  if (paused) return isTaskNotificationSuperseded(paused, tasks) ? null : paused;
  if (!observedTaskId) return null;
  const observed = tasks.find((task) => task.operation_id === observedTaskId);
  if (!observed || isTaskNotificationSuperseded(observed, tasks)) return null;
  const byId = new Map(tasks.map((task) => [task.operation_id, task]));
  const descendants = tasks
    .filter((task) => task.operation_id !== observed.operation_id)
    .filter((task) => hasAncestor(task, observed.operation_id, byId))
    .sort(compareTaskTime);
  const latestDescendant = descendants.at(-1);
  if (latestDescendant) {
    if (latestDescendant.settled) return null;
    return latestDescendant;
  }
  return observed;
}

export function taskKindLabel(kind: AgentTaskKind): string {
  if (kind === "artifact_edit") return "Artifact edit";
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
    case "auto_research":
      return "Auto-research";
    case "branch_merge":
      return "Branch merge";
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
      (task) =>
        (task.kind === kind ||
          (task.kind === "artifact_edit" &&
            Boolean(task.request.chat_id) &&
            task.request.chat_scope === (kind === "project_chat" ? "project" : "node"))) &&
        (kind === "project_chat" || task.request.node_id === nodeId),
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
  const continuedTaskIds = new Set(
    tasks.flatMap((task) => (task.parent_operation_id ? [task.parent_operation_id] : [])),
  );
  return (
    [...tasks]
      .reverse()
      .find((task) => task.paused && task.can_resume && !continuedTaskIds.has(task.operation_id)) ??
    null
  );
}

export function chatTasksMissingFromHistory(
  tasks: AgentTask[],
  messages: ChatMessage[],
): AgentTask[] {
  const persistedOperationIds = new Set(
    messages.flatMap((message) =>
      message.operation_id && !message.steering ? [message.operation_id] : [],
    ),
  );
  // Operation id is the turn identity. Matching by prompt text loses one of
  // two legitimate turns when the human sends the same message twice.
  return tasks.filter((task) => !persistedOperationIds.has(task.operation_id));
}

export function chatMessageTranscriptLine(message: ChatMessage): TaskTranscriptLine {
  return {
    lineId: `message:${message.message_id}`,
    role: message.role === "user" && message.trigger !== "watcher" ? "human" : "agent",
    text: message.text,
    taskId: message.operation_id ?? message.message_id,
    timestamp: message.timestamp,
    mode: message.mode,
    trigger: message.trigger,
    graphUpdate: message.graph_update,
    attachments: message.attachments,
    steering: message.steering,
    browserStatus: message.steering ? null : message.browser_status,
  };
}

export function reconcileChatHistoryArtifacts(
  messages: ChatMessage[],
  tasks: AgentTask[],
): TaskTranscriptLine[] {
  const lines = messages.map(chatMessageTranscriptLine);
  // Prefer the answer; a failed turn may only have its human request in history.
  const browserLineByOperation = new Map<string, TaskTranscriptLine>();
  for (const line of lines) {
    if (!line.browserStatus) continue;
    const previous = browserLineByOperation.get(line.taskId);
    if (!previous || (previous.role === "human" && line.role === "agent")) {
      if (previous) previous.browserStatus = null;
      browserLineByOperation.set(line.taskId, line);
    } else line.browserStatus = null;
  }
  const answerLineByOperationId = new Map<string, number>();
  messages.forEach((message, index) => {
    if (
      message.role === "assistant" &&
      message.operation_id &&
      !answerLineByOperationId.has(message.operation_id)
    ) {
      answerLineByOperationId.set(message.operation_id, index);
    }
  });
  tasks.forEach((task) => {
    const artifacts = taskArtifacts(task);
    const artifactOmissions = taskArtifactOmissions(task);
    const lineIndex = answerLineByOperationId.get(task.operation_id);
    if (lineIndex !== undefined) {
      lines[lineIndex] = {
        ...lines[lineIndex],
        ...(artifacts.length ? { artifacts } : {}),
        ...(artifactOmissions ? { artifactOmissions } : {}),
      };
      return;
    }
    // A steer can persist the human message before its answer. Preserve that
    // attempt, and any deliverables from a turn with no persisted answer.
    const operationMessages = messages.filter(
      (message) => message.operation_id === task.operation_id,
    );
    if (
      operationMessages.some((message) => message.role === "user" && !message.steering) &&
      (operationMessages.some((message) => message.steering) ||
        artifacts.length > 0 ||
        artifactOmissions !== undefined)
    ) {
      lines.push(...reconstructTaskTranscript([task]).filter((line) => line.role !== "human"));
    }
  });
  return lines;
}

export function reconstructTaskTranscript(tasks: AgentTask[]): TaskTranscriptLine[] {
  return [...tasks].sort(compareTaskTime).flatMap((task) => {
    const lines: TaskTranscriptLine[] = [];
    const message = textValue(task.request.message);
    const mode = conversationMode(task.request.mode);
    const trigger = taskTrigger(task.request.trigger);
    const graphUpdate = task.result?.graph_update ?? null;
    if (message && trigger === "human") {
      const attachments = taskAttachments(task.request.attachments);
      lines.push({
        lineId: `task:${task.operation_id}:human`,
        role: "human",
        text: message,
        taskId: task.operation_id,
        timestamp: task.created_at,
        mode,
        trigger,
        ...(attachments.length ? { attachments } : {}),
      });
    }
    const messages = Array.isArray(task.result?.messages)
      ? task.result.messages.filter(
          (item): item is string => typeof item === "string" && item.trim().length > 0,
        )
      : [];
    const artifacts = taskArtifacts(task);
    const artifactOmissions = taskArtifactOmissions(task);
    messages.forEach((text, index) =>
      lines.push({
        lineId: `task:${task.operation_id}:answer:${index}`,
        role: "agent",
        text,
        taskId: task.operation_id,
        timestamp: task.created_at,
        mode,
        trigger,
        ...(index === messages.length - 1 && artifacts.length ? { artifacts } : {}),
        ...(index === messages.length - 1 && artifactOmissions ? { artifactOmissions } : {}),
        ...(index === messages.length - 1 && graphUpdate ? { graphUpdate } : {}),
      }),
    );
    if (
      !messages.length &&
      (artifacts.length || artifactOmissions || (graphUpdate && graphUpdate.status !== "none"))
    ) {
      lines.push({
        lineId: `task:${task.operation_id}:deliverables`,
        role: "agent",
        text: "",
        taskId: task.operation_id,
        timestamp: task.created_at,
        mode,
        trigger,
        ...(artifacts.length ? { artifacts } : {}),
        ...(artifactOmissions ? { artifactOmissions } : {}),
        ...(graphUpdate ? { graphUpdate } : {}),
      });
    }
    const graphOnlyRejection = task.settled && graphUpdate?.status === "rejected";
    if (task.error && !graphOnlyRejection) {
      lines.push({
        lineId: `task:${task.operation_id}:error`,
        role: "error",
        text: task.error,
        taskId: task.operation_id,
        timestamp: task.created_at,
        trigger,
      });
    } else if (task.awaiting_human) {
      lines.push({
        lineId: `task:${task.operation_id}:status`,
        role: task.failed ? "error" : "meta",
        text: task.status_message,
        taskId: task.operation_id,
        timestamp: task.created_at,
        trigger,
      });
    }
    // A watcher wake or episode turn has no human line to carry its progress,
    // so until it produces output the chat would show nothing at all.
    if (!lines.length && trigger !== "human" && task.active) {
      lines.push({
        lineId: `task:${task.operation_id}:running`,
        role: "agent",
        text: "",
        taskId: task.operation_id,
        timestamp: task.created_at,
        mode,
        trigger,
        running: true,
      });
    }
    return lines;
  });
}

function taskAttachments(value: unknown): ChatAttachmentDescriptor[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (item): item is ChatAttachmentDescriptor =>
      typeof item === "object" &&
      item !== null &&
      typeof item.attachment_id === "string" &&
      typeof item.name === "string" &&
      typeof item.media_type === "string" &&
      typeof item.size === "number" &&
      typeof item.expires_at === "string",
  );
}

export function orderTranscriptLines(lines: TaskTranscriptLine[]): TaskTranscriptLine[] {
  return [...lines].sort(
    (left, right) => comparableTime(left.timestamp) - comparableTime(right.timestamp),
  );
}

export function artifactUrl(
  projectId: string,
  taskId: string,
  artifactId: string,
  action: "content" | "preview" | "viewer" | "download" | "keep",
): string {
  return `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(taskId)}/artifacts/${encodeURIComponent(artifactId)}/${action}`;
}

export function versionedArtifactContentUrl(
  projectId: string,
  taskId: string,
  artifactId: string,
  taskUpdatedAt: string,
): string {
  return `${artifactUrl(projectId, taskId, artifactId, "content")}?task_updated_at=${encodeURIComponent(taskUpdatedAt)}`;
}

/** The server resolves the chat binding; task or transcript session ids are history. */
export function resolvedChatSessionId(tasks: AgentTask[]): string | null {
  return [...tasks].sort(compareTaskTime).at(-1)?.current_chat_session_id ?? null;
}

function compareTaskTime(left: AgentTask, right: AgentTask): number {
  return (
    Date.parse(left.created_at) - Date.parse(right.created_at) ||
    left.operation_id.localeCompare(right.operation_id)
  );
}

function comparableTime(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function hasAncestor(
  task: AgentTask,
  ancestorId: string,
  byId: ReadonlyMap<string, AgentTask>,
): boolean {
  const seen = new Set<string>();
  let parentId = task.parent_operation_id;
  while (parentId && !seen.has(parentId)) {
    if (parentId === ancestorId) return true;
    seen.add(parentId);
    parentId = byId.get(parentId)?.parent_operation_id ?? null;
  }
  return false;
}

function textValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function conversationMode(value: unknown): ConversationMode | null {
  return value === "discuss" || value === "work" ? value : null;
}

function taskTrigger(value: unknown): TaskTrigger {
  return value === "experiment_run" || value === "watcher" ? value : "human";
}

export function taskArtifacts(task: AgentTask): AgentArtifactDescriptor[] {
  if (!Array.isArray(task.result?.artifacts)) return [];
  return task.result.artifacts.filter(
    (item): item is AgentArtifactDescriptor =>
      typeof item === "object" &&
      item !== null &&
      typeof item.artifact_id === "string" &&
      typeof item.name === "string" &&
      typeof item.media_type === "string" &&
      ["html", "image", "markdown", "text", "pdf", "file"].includes(item.view) &&
      typeof item.available === "boolean" &&
      (item.unavailable_reason === null || typeof item.unavailable_reason === "string") &&
      typeof item.can_open === "boolean" &&
      typeof item.can_download === "boolean" &&
      typeof item.can_keep === "boolean" &&
      typeof item.can_discuss === "boolean",
  );
}

export function taskArtifactOmissions(task: AgentTask): ArtifactOmissions | undefined {
  const value = task.result?.artifact_omissions;
  if (!value || typeof value !== "object" || typeof value.discovery_failed !== "boolean") {
    return undefined;
  }
  const omissions: ArtifactOmissions = { discovery_failed: value.discovery_failed };
  for (const reason of [
    "count_limit",
    "file_size_limit",
    "total_size_limit",
    "empty",
    "invalid_or_unavailable",
  ] as const) {
    const count = value[reason];
    if (typeof count === "number" && Number.isSafeInteger(count) && count >= 0) {
      omissions[reason] = count;
    }
  }
  return omissions.discovery_failed ||
    Object.values(omissions).some((count) => typeof count === "number" && count > 0)
    ? omissions
    : undefined;
}
