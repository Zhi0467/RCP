import { memberDraftKey } from "../core/draftStorage.ts";
import { MAIN_GRAPH, projectViewHash, sameGraphTarget } from "../core/graphTarget.ts";
import type { ComposerTurnFields } from "./composerAttachments.tsx";
import type {
  AgentRunConfig,
  AgentTask,
  AgentTaskKind,
  AgentTaskRequest,
  AgentTaskStatus,
  Episode,
  ArtifactContextRequest,
  ChatAgentSetup,
  ChatMessage,
  ChatOrigin,
  ChatReads,
  ChatSummary,
  ConversationMode,
  GraphTargetRef,
  SkillDefaults,
  WorktreeIntegrationOption,
} from "../core/types";

/** Mirrors rcp.limits.AGENT_NAME_MAX_CHARS: one cap for a conversation's stored agent name. */
export const AGENT_NAME_MAX_CHARS = 35;

export type ChatKind = "node_chat" | "project_chat";

export interface ChatConversation {
  chatId: string;
  kind: ChatKind;
  nodeId: string | null;
  title: string;
  tasks: AgentTask[];
  updatedAt: string;
  /** The latest message's text from the stored summary, for search. */
  preview?: string;
  /** The newest recorded turn's setup, from the stored summary. */
  agentSetup?: ChatSummary["agent_setup"];
  graphTarget?: GraphTargetRef;
  origin?: ChatSummary["origin"];
  conversationKind?: ChatSummary["conversation_kind"];
  orchestratorEpisodeId?: string | null;
  humanTurnRefusal?: ChatSummary["human_turn_refusal"];
  /** Set from a server projection that says a human is owed an answer, such as an open question. */
  needsHuman?: boolean;
}

/** Whether messaging the orchestrator can lift a child chat's lock; isolation and merge refusals bind it too. */
export function orchestratorCanUnlock(refusal: ChatConversation["humanTurnRefusal"]): boolean {
  return refusal?.code === "auto_research_child_read_only";
}

export interface DraftConversation {
  chatId: string;
  kind: ChatKind;
  nodeId: string | null;
  title: string;
}

export function chatDraftStorageKey(
  actorId: string | null,
  projectId: string,
  chatId: string,
): string | null {
  return memberDraftKey(actorId, `chat-draft:${projectId}:${chatId}`);
}

export function chatModeStorageKey(projectId: string, chatId: string): string {
  return `rcp:chat-mode:${projectId}:${chatId}`;
}

export function parseConversationMode(value: unknown): ConversationMode | null {
  return value === "discuss" || value === "work" ? value : null;
}

export function toggleConversationMode(mode: ConversationMode): ConversationMode {
  return mode === "discuss" ? "work" : "discuss";
}

export function isConversationModeShortcut(key: string, shiftKey: boolean): boolean {
  return key === "Tab" && shiftKey;
}

export function latestPersistedConversationMode(
  messages: ChatMessage[],
  tasks: AgentTask[],
): ConversationMode {
  const candidates: Array<{ mode: ConversationMode; timestamp: number; order: number }> = [];
  let order = 0;
  messages.forEach((message) => {
    const mode = parseConversationMode(message.mode);
    if (mode) candidates.push({ mode, timestamp: comparableTime(message.timestamp), order });
    order += 1;
  });
  tasks.forEach((task) => {
    const mode = parseConversationMode(task.request.mode);
    if (mode) candidates.push({ mode, timestamp: comparableTime(task.created_at), order });
    order += 1;
  });
  candidates.sort((left, right) => left.timestamp - right.timestamp || left.order - right.order);
  return candidates.at(-1)?.mode ?? "discuss";
}

/** The transcript records "use the provider default" as a literal sentinel; a
 *  request records it as an empty string. Continuing a conversation reads the
 *  transcript, so the sentinel has to be translated back or it is sent to the
 *  provider as though it were a real model name. */
function persistedModel(value: string | null | undefined): string {
  return !value || value === "provider-default" ? "" : value;
}

/** A provider other than the one holding this chat's current session cannot
 *  resume it, so the backend starts the next turn in a fresh provider session.
 *  The holder is the task that produced that session, not the latest request,
 *  which may have failed before it got one. Model and effort changes keep it. */
export function providerSwitchStartsFreshSession(
  tasks: AgentTask[],
  sessionId: string | null | undefined,
  nextProvider: string,
): boolean {
  if (!sessionId) return false;
  const holder = tasks.find((task) => task.native_session_id === sessionId);
  const provider = holder?.request.provider;
  return typeof provider === "string" && provider !== nextProvider;
}

/**
 * What a chat's agent runs on: the newer of its loaded turns and the chat
 * summary's newest turn, else `fallback` (the chat it was branched from, or the
 * project default). A turn that names no machine or effort keeps the fallback's.
 * An artifact edit never sets it: an origin-bound one runs the artifact's own
 * agent, and any other runs the chat's. History is read only without a summary
 * setup, since its records cannot tell such an edit from a turn.
 */
export function latestPersistedChatConfig(
  messages: ChatMessage[],
  tasks: AgentTask[],
  fallback: AgentRunConfig,
  setup?: ChatAgentSetup | null,
): AgentRunConfig {
  const candidates: Array<{ config: AgentRunConfig; timestamp: number; order: number }> = [];
  const add = (
    turn: { provider?: unknown; model?: unknown; reasoning?: unknown; run_on?: unknown },
    at: string,
  ) => {
    const order = candidates.length;
    if (typeof turn.provider !== "string" || !turn.provider.trim()) return;
    candidates.push({
      config: {
        provider: turn.provider,
        model: typeof turn.model === "string" ? persistedModel(turn.model) : "",
        reasoning:
          typeof turn.reasoning === "string" && turn.reasoning
            ? turn.reasoning
            : fallback.reasoning,
        run_on:
          typeof turn.run_on === "string" && turn.run_on.trim() ? turn.run_on : fallback.run_on,
      },
      timestamp: comparableTime(at),
      order,
    });
  };
  if (!setup)
    messages.forEach((message) =>
      add({ ...message, run_on: message.execution_machine }, message.timestamp),
    );
  tasks.forEach((task) => {
    if (task.kind !== "artifact_edit") add(task.request, task.created_at);
  });
  if (setup) add(setup, setup.selected_at);
  candidates.sort((left, right) => left.timestamp - right.timestamp || left.order - right.order);
  return candidates.at(-1)?.config ?? fallback;
}

/** A branched chat's recorded setup as a run config, else the project default. */
export function originRunConfig(
  origin: ChatOrigin | null | undefined,
  profile: AgentRunConfig,
): AgentRunConfig {
  return origin
    ? {
        provider: origin.provider,
        model: origin.model ?? "",
        reasoning: origin.reasoning ?? "",
        run_on: origin.run_on,
      }
    : profile;
}

/** What an agent runs on, for its card and list row; the composer reads the same with its full history. */
export function conversationAgentConfig(
  conversation: ChatConversation,
  profile: AgentRunConfig,
): AgentRunConfig {
  return latestPersistedChatConfig(
    [],
    conversation.tasks,
    originRunConfig(conversation.origin, profile),
    conversation.agentSetup,
  );
}

export interface ConversationTurnSubmission {
  kind: ChatKind;
  config: AgentRunConfig;
  nodeId: string | null;
  message: string;
  chatId: string;
  sessionId: string | null;
  mode: ConversationMode;
  artifactContext?: ArtifactContextRequest | null;
  /** The composer's staged files and references, as `turnFields()` gives them. */
  attachments?: ComposerTurnFields;
  skills?: SkillDefaults;
  providerSkillNames?: string[];
  /** The repository a first worktree turn checks out; the server stamps every other scope. */
  worktreeRepository?: string | null;
  worktree?: boolean;
  worktreeIntegration?: WorktreeIntegrationOption["id"];
}

export function conversationTurnRequest(submission: ConversationTurnSubmission): AgentTaskRequest {
  const message = submission.message.trim();
  // An artifact comment turn may carry only its comments; the server writes their text.
  if (!message && !submission.artifactContext?.selections.length)
    throw new Error("A conversation turn requires a non-blank message.");
  const skills = submission.skills ?? { workflow_ids: [], skill_ids: [] };
  return {
    ...submission.config,
    model: submission.config.model || null,
    node_id: submission.nodeId,
    message,
    chat_id: submission.chatId,
    session_id: submission.sessionId,
    mode: submission.mode,
    ...(submission.worktree
      ? {
          worktree: true,
          ...(submission.worktreeRepository
            ? { run_truth_scope: [submission.worktreeRepository] }
            : {}),
        }
      : {}),
    ...(submission.worktreeIntegration
      ? { worktree_integration: submission.worktreeIntegration }
      : {}),
    ...submission.attachments,
    ...(submission.artifactContext ? { artifact_context: submission.artifactContext } : {}),
    invoked_workflow_ids: skills.workflow_ids,
    invoked_skill_ids: skills.skill_ids,
    invoked_provider_skill_names: submission.providerSkillNames ?? [],
  };
}

/** Starts a task; `requestId` is the start's Idempotency-Key, so sending the
 *  same start again after an unanswered request admits at most one turn. A chat
 *  turn posts to the viewed graph unless `graphTarget` names the conversation's own. */
export type StartConversationTask = (
  kind: AgentTaskKind,
  request: AgentTaskRequest,
  requestId?: string,
  graphTarget?: GraphTargetRef,
) => Promise<AgentTask>;

/** One ordinary Discuss/Work dispatch boundary shared by the visible composer
 * and non-UI callers. Admission and durable task recording remain in App's
 * start-task owner. */
export async function startConversationTurn(
  startTask: StartConversationTask,
  submission: ConversationTurnSubmission,
  requestId?: string,
): Promise<AgentTask> {
  return startTask(submission.kind, conversationTurnRequest(submission), requestId);
}

/** An Auto-research orchestrator turn, or an artifact edit replying to its chain:
 *  the chain's conversation is named by the chain root, which is also the branch
 *  every chain member writes. */
function orchestratorChatId(task: AgentTask): string | null {
  const edit = task.request.artifact_edit as { reply_episode_id?: string | null } | undefined;
  const turn =
    (task.kind === "auto_research" && task.request.role === "orchestrator") ||
    (task.kind === "artifact_edit" && Boolean(edit?.reply_episode_id));
  if (!turn) return null;
  return task.graph_target?.kind === "branch" ? (task.graph_target.branch_id ?? null) : null;
}

export function chatIdForTask(task: AgentTask): string | null {
  const orchestrator = orchestratorChatId(task);
  if (orchestrator) return orchestrator;
  if (task.kind !== "node_chat" && task.kind !== "project_chat" && task.kind !== "artifact_edit")
    return null;
  const value = task.request.chat_id;
  return typeof value === "string" && value.trim() ? value : null;
}

export function groupChatConversations(
  summaries: ChatSummary[],
  tasks: AgentTask[],
  nodeTitles: Record<string, string>,
  projectTitle: string,
  drafts: DraftConversation[] = [],
  viewedTarget: GraphTargetRef = MAIN_GRAPH,
): ChatConversation[] {
  const grouped = new Map<string, ChatConversation>();
  for (const summary of summaries) {
    // The agent's one stored name; several agents can share a node.
    grouped.set(summary.chat_id, {
      chatId: summary.chat_id,
      kind: summary.kind,
      nodeId: summary.node_id,
      title: summary.title,
      tasks: [],
      updatedAt: summary.updated_at,
      preview: summary.last_message_preview,
      agentSetup: summary.agent_setup,
      graphTarget: summary.graph_target,
      origin: summary.origin,
      conversationKind: summary.conversation_kind,
      orchestratorEpisodeId: summary.orchestrator_episode_id,
      humanTurnRefusal: summary.human_turn_refusal,
    });
  }
  for (const draft of drafts) {
    if (!grouped.has(draft.chatId))
      grouped.set(draft.chatId, {
        ...draft,
        graphTarget: viewedTarget,
        conversationKind: "chat",
        tasks: [],
        updatedAt: "",
      });
  }
  for (const task of tasks) {
    const chatId = chatIdForTask(task);
    if (!chatId) continue;
    const orchestrator = orchestratorChatId(task) !== null;
    const kind: ChatKind = orchestrator
      ? "project_chat"
      : task.kind !== "artifact_edit"
        ? (task.kind as ChatKind)
        : task.request.chat_scope === "project"
          ? "project_chat"
          : "node_chat";
    const nodeId =
      kind === "node_chat" && typeof task.request.node_id === "string"
        ? task.request.node_id
        : null;
    const existing = grouped.get(chatId);
    const title = nodeId ? (nodeTitles[nodeId] ?? nodeId) : projectTitle;
    if (existing) {
      existing.tasks.push(task);
      if (Date.parse(task.updated_at) > Date.parse(existing.updatedAt || "1970-01-01")) {
        existing.updatedAt = task.updated_at;
      }
    } else {
      grouped.set(chatId, {
        chatId,
        kind,
        nodeId,
        title,
        tasks: [task],
        updatedAt: task.updated_at,
        graphTarget: task.graph_target,
        // Ownership is unknown until the inventory lists this conversation.
        conversationKind: orchestrator ? "auto_research" : undefined,
      });
    }
  }
  grouped.forEach((conversation) =>
    conversation.tasks.sort(
      (left, right) =>
        Date.parse(left.created_at) - Date.parse(right.created_at) ||
        left.operation_id.localeCompare(right.operation_id),
    ),
  );
  return [...grouped.values()].sort(
    (left, right) =>
      Date.parse(right.updatedAt || "9999-01-01") - Date.parse(left.updatedAt || "9999-01-01") ||
      left.title.localeCompare(right.title),
  );
}

/**
 * The orchestrator conversation as NodeChat reads a chat: each loaded orchestrator
 * turn or artifact edit becomes a project-chat turn carrying the human input its
 * transcript line recorded (or the starting instruction; an ordinary chat turn
 * keeps its own kind and message), so status and progress
 * come from the live task; the transcript keeps only turns the bounded task list
 * no longer holds.
 */
export function orchestratorChatTurns(
  conversation: ChatConversation,
  history: readonly ChatMessage[],
): { tasks: AgentTask[]; history: ChatMessage[] } {
  const loaded = new Set(conversation.tasks.map((task) => task.operation_id));
  const humanText = new Map(
    history.flatMap((message) =>
      message.role === "user" && message.operation_id
        ? [[message.operation_id, message.text] as const]
        : [],
    ),
  );
  return {
    tasks: conversation.tasks.map((task) => {
      // An ordinary turn posted after the chain ended carries its own message.
      const input = task.kind === "project_chat" ? task.request.message : task.request.instruction;
      const message =
        humanText.get(task.operation_id) ??
        (typeof input === "string" && input.trim() ? input : null);
      return {
        ...task,
        // An artifact edit stays an edit, so it never takes a turn's Retry.
        kind: task.kind === "artifact_edit" ? "artifact_edit" : "project_chat",
        request: {
          ...task.request,
          chat_id: conversation.chatId,
          chat_scope: "project",
          message,
          trigger: message ? "human" : "orchestrator",
        },
      };
    }),
    history: history.filter(
      (message) => !message.operation_id || !loaded.has(message.operation_id),
    ),
  };
}

/** What a conversation's latest turn asks of the human, for the Agents panel. */
export type ConversationAgentState = "stopped" | "working" | "unread" | "failed" | "done" | "draft";
export type ConversationAgentGroup = "new_reply" | "failed" | "stopped" | "working" | "done";
export interface ConversationAgentStatus {
  state: ConversationAgentState;
  group: ConversationAgentGroup;
  latest: AgentTask | null;
  unread: boolean;
}

export const CONVERSATION_AGENT_GROUPS: readonly ConversationAgentGroup[] = [
  "new_reply",
  "failed",
  "stopped",
  "working",
  "done",
];

export function conversationAgentStatus(
  conversation: ChatConversation,
  unreadChatIds: ReadonlySet<string>,
): ConversationAgentStatus {
  const latest = conversation.tasks.at(-1) ?? null;
  const unread = conversationHasUnread(conversation, unreadChatIds);
  const result = { latest, unread };
  if (orchestratorOwnsChild(conversation)) {
    const state = latest?.active ? "working" : "done";
    return { ...result, state, group: state };
  }
  if (latest?.failed) return { ...result, state: "failed", group: "failed" };
  // After failures, the backend attention flag covers paused and interrupted turns.
  if (latest?.paused || latest?.awaiting_human || conversation.needsHuman) {
    return { ...result, state: "stopped", group: "stopped" };
  }
  if (latest?.active) return { ...result, state: "working", group: "working" };
  if (!latest) {
    if (unread) return { ...result, state: "unread", group: "new_reply" };
    return { ...result, state: conversation.updatedAt ? "done" : "draft", group: "done" };
  }
  if (latest.settled && unread) return { ...result, state: "unread", group: "new_reply" };
  return { ...result, state: "done", group: "done" };
}

/** The list's sections: pinned conversations first, then each status group. */
export type AgentListSection = "pinned" | ConversationAgentGroup;
export const AGENT_LIST_SECTIONS: readonly AgentListSection[] = [
  "pinned",
  ...CONVERSATION_AGENT_GROUPS,
];

export interface ConversationAgentRow {
  conversation: ChatConversation;
  status: ConversationAgentStatus;
}

/**
 * Everything the browser already holds about a conversation, lower-cased: its
 * name, node, chat kind, latest message, and each loaded turn's prompt,
 * provider, model, effort, and repositories. Every search word must appear.
 */
export function conversationSearchText(conversation: ChatConversation): string {
  const parts: (string | null | undefined)[] = [
    conversation.title,
    conversation.nodeId,
    conversation.kind === "project_chat" ? "project chat" : "node chat",
    conversation.preview,
    conversation.agentSetup?.provider,
    conversation.agentSetup?.model,
    conversation.agentSetup?.reasoning,
  ];
  for (const task of conversation.tasks) {
    const request = task.request;
    parts.push(
      typeof request.message === "string" ? request.message : null,
      task.provider_label,
      request.provider,
      request.model,
      request.reasoning,
      ...(request.run_truth_scope ?? []),
    );
  }
  return parts.filter(Boolean).join("\n").toLocaleLowerCase();
}

/**
 * Rows grouped by latest turn status; each group keeps recency order. Pinned
 * conversations leave their status group for the pinned section, newest pin first.
 */
export function groupConversationAgents(
  conversations: ChatConversation[],
  unreadChatIds: ReadonlySet<string>,
  query = "",
  pinnedChatIds: readonly string[] = [],
): Record<AgentListSection, ConversationAgentRow[]> {
  const terms = query.toLocaleLowerCase().split(/\s+/).filter(Boolean);
  const pinOrder = new Map(pinnedChatIds.map((chatId, index) => [chatId, index]));
  const groups: Record<AgentListSection, ConversationAgentRow[]> = {
    pinned: [],
    new_reply: [],
    failed: [],
    stopped: [],
    working: [],
    done: [],
  };
  for (const conversation of conversations) {
    if (terms.length) {
      const text = conversationSearchText(conversation);
      if (!terms.every((term) => text.includes(term))) continue;
    }
    const status = conversationAgentStatus(conversation, unreadChatIds);
    groups[pinOrder.has(conversation.chatId) ? "pinned" : status.group].push({
      conversation,
      status,
    });
  }
  groups.pinned.sort(
    (left, right) =>
      (pinOrder.get(left.conversation.chatId) ?? 0) -
      (pinOrder.get(right.conversation.chatId) ?? 0),
  );
  return groups;
}

/** A draft nobody has sent a turn in; opening a new chat reuses it. */
export function unsentConversation(
  conversations: ChatConversation[],
  kind: ChatKind,
  nodeId: string | null = null,
): ChatConversation | null {
  return (
    conversations.find(
      (conversation) =>
        conversation.kind === kind &&
        conversation.nodeId === nodeId &&
        conversation.tasks.length === 0 &&
        !conversation.updatedAt,
    ) ?? null
  );
}

export function latestConversation(
  conversations: ChatConversation[],
  kind: ChatKind,
  nodeId: string | null = null,
  graphTarget: GraphTargetRef = MAIN_GRAPH,
): ChatConversation | null {
  return (
    conversations.find(
      (conversation) =>
        sameGraphTarget(conversation.graphTarget, graphTarget) &&
        conversation.conversationKind !== "auto_research_child" &&
        conversation.conversationKind !== "auto_research" &&
        conversation.kind === kind &&
        (kind === "project_chat" || conversation.nodeId === nodeId),
    ) ?? null
  );
}

/** All conversation entry points exclude archived chats before considering reuse. */
export function pickOrCreateConversation(
  conversations: ChatConversation[],
  archivedIds: readonly string[],
  kind: ChatKind,
  nodeId: string | null,
  create: () => string,
  {
    reuse = "latest",
    graphTarget = MAIN_GRAPH,
  }: {
    reuse?: "latest" | "draft";
    graphTarget?: GraphTargetRef;
  } = {},
): string {
  const archived = new Set(archivedIds);
  const available = conversations.filter(
    (conversation) =>
      !archived.has(conversation.chatId) && sameGraphTarget(conversation.graphTarget, graphTarget),
  );
  const selected =
    reuse === "draft"
      ? unsentConversation(available, kind, nodeId)
      : latestConversation(available, kind, nodeId, graphTarget);
  return selected?.chatId ?? create();
}

export function listedAgentConversations(
  conversations: ChatConversation[],
  graphTarget: GraphTargetRef,
  orchestratorBranchIds: ReadonlySet<string> = new Set(),
): ChatConversation[] {
  // Main also lists each branch orchestrator whose current episode is known, so its
  // card keeps attention and a composer; a branch lists only its own agents.
  return conversations.filter(
    (conversation) =>
      sameGraphTarget(conversation.graphTarget, graphTarget) ||
      (graphTarget.kind === "main" &&
        conversation.conversationKind === "auto_research" &&
        orchestratorBranchIds.has(conversation.chatId)),
  );
}

export function chatIndicator(
  conversations: ChatConversation[],
  unreadChatIds: ReadonlySet<string>,
): "active" | "unread" | null {
  if (
    conversations.some(
      (conversation) =>
        conversation.needsHuman ||
        (!orchestratorOwnsChild(conversation) && conversation.tasks.some(chatTaskNeedsAttention)),
    )
  )
    return "active";
  return unreadChatIds.size > 0 ? "unread" : null;
}

/** The episode currently writing an orchestrator conversation's branch. */
export function chainEpisode(
  conversation: ChatConversation,
  episodes: readonly Episode[],
): Episode | null {
  if (conversation.conversationKind !== "auto_research") return null;
  return episodes.find((episode) => episode.graph_target.branch_id === conversation.chatId) ?? null;
}

/** An orchestrator whose chain needs action needs the human even with settled, read turns. */
export function withEpisodeAttention(
  conversations: ChatConversation[],
  episodes: readonly Episode[],
): ChatConversation[] {
  return conversations.map((conversation) =>
    chainEpisode(conversation, episodes)?.health === "needs_action"
      ? { ...conversation, needsHuman: true }
      : conversation,
  );
}

export function chatTaskNeedsAttention(task: AgentTask): boolean {
  return task.active || task.paused;
}

export function chatEntryConversationId(
  conversations: ChatConversation[],
  activityTask: AgentTask | null,
  unreadChatIds: ReadonlySet<string>,
  previousChatId: string | null,
): string | null {
  if (previousChatId && conversations.some((item) => item.chatId === previousChatId))
    return previousChatId;
  const activeChatId =
    activityTask && chatTaskNeedsAttention(activityTask) ? chatIdForTask(activityTask) : null;
  if (activeChatId && conversations.some((item) => item.chatId === activeChatId))
    return activeChatId;
  const unread = conversations.find((conversation) => unreadChatIds.has(conversation.chatId));
  if (unread) return unread.chatId;
  return conversations[0]?.chatId ?? null;
}

/** Chat turns this page saw change into a settled state; their summaries are stale. */
export function newlyFinishedChatTaskIds(
  tasks: AgentTask[],
  previousStatuses: ReadonlyMap<string, AgentTaskStatus>,
): string[] {
  return tasks.flatMap((task) => {
    const chatId = chatIdForTask(task);
    const previous = previousStatuses.get(task.operation_id);
    const becameTerminal =
      previous !== undefined && previous !== task.status && !chatTaskNeedsAttention(task);
    return chatId && becameTerminal ? [task.operation_id] : [];
  });
}

/** Chat tasks whose steer revision moved since last seen, such as a teammate's queued steer. */
export function newlySteeredChatTaskIds(
  tasks: AgentTask[],
  previousRevisions: ReadonlyMap<string, number>,
): string[] {
  return tasks.flatMap((task) => {
    // A turn first seen with steers already queued counts from zero.
    const moved = (previousRevisions.get(task.operation_id) ?? 0) !== (task.steer_revision ?? 0);
    return chatIdForTask(task) && moved ? [task.operation_id] : [];
  });
}

function laterTime(left: string | null | undefined, right: string | null | undefined) {
  if (!left) return right ?? null;
  if (!right) return left;
  return Date.parse(right) > Date.parse(left) ? right : left;
}

/**
 * Each chat's newest turn end: the server's durable answer, advanced by any
 * loaded turn that finished since it was read.
 */
function latestChatFinishes(
  tasks: readonly AgentTask[],
  chatReads: ChatReads,
): Record<string, string> {
  const latest: Record<string, string> = { ...chatReads.latest_finished };
  const archived = new Set(chatReads.archived);
  for (const task of tasks) {
    const chatId = chatIdForTask(task);
    if (!chatId || archived.has(chatId)) continue;
    if (!task.finished || !task.finished_at || chatTaskNeedsAttention(task)) continue;
    latest[chatId] = laterTime(latest[chatId], task.finished_at) ?? task.finished_at;
  }
  return latest;
}

/** A chat is unread when its newest turn ended after the viewer's marker for it. */
export function unreadChatIdsFromReads(
  tasks: readonly AgentTask[],
  chatReads: ChatReads | null,
): Set<string> {
  if (!chatReads) return new Set();
  const latest = latestChatFinishes(tasks, chatReads);
  return new Set(
    Object.keys(latest).filter(
      (chatId) =>
        Date.parse(latest[chatId]) > Date.parse(chatReads.reads[chatId] ?? chatReads.baseline),
    ),
  );
}

/** The newest turn end in a chat, which is what reading it marks. */
export function chatReadThrough(
  tasks: readonly AgentTask[],
  chatReads: ChatReads,
  chatId: string,
): string | null {
  return latestChatFinishes(tasks, chatReads)[chatId] ?? null;
}

/** Markers only move forward, so a stale snapshot cannot unread a viewed reply. */
export function mergeChatReads(current: ChatReads | null, incoming: ChatReads): ChatReads {
  if (!current) return incoming;
  const reads = { ...incoming.reads };
  for (const [chatId, readThrough] of Object.entries(current.reads)) {
    reads[chatId] = laterTime(reads[chatId], readThrough) ?? readThrough;
  }
  return { ...incoming, reads };
}

export function conversationHasUnread(
  conversation: ChatConversation,
  unreadChatIds: ReadonlySet<string>,
): boolean {
  return !orchestratorOwnsChild(conversation) && unreadChatIds.has(conversation.chatId);
}

/** A child's attention belongs to its orchestrator only while the lineage keeps it read-only. */
function orchestratorOwnsChild(conversation: ChatConversation): boolean {
  return (
    conversation.conversationKind === "auto_research_child" &&
    conversation.humanTurnRefusal?.code === "auto_research_child_read_only"
  );
}

function comparableTime(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}

/** Inventory rows enter the existing route so the target loads before the chat. */
export function conversationHref(projectId: string, conversation: ChatConversation): string {
  return projectViewHash(projectId, conversation.graphTarget ?? MAIN_GRAPH, "chats", {
    chatId: conversation.chatId,
  });
}
