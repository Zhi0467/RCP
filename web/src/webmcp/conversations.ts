import { sameGraphTarget } from "../core/graphTarget";
import type {
  GraphTargetRef,
  AgentRunConfig,
  AgentTask,
  ChatMessage,
  ChatSummary,
  ChatTranscript,
  GraphNode,
  ProjectSnapshot,
} from "../core/types";
import {
  resolvedChatSessionId,
  relatedChatTasks,
  resumablePausedChatTask,
  taskArtifacts,
} from "../agents/agentTasks";
import {
  latestPersistedChatConfig,
  type ChatKind,
  type ConversationTurnSubmission,
} from "../chat/chatWorkspace";
import { filterSkillCatalogToDefaults } from "../core/skillPickerModel";
import {
  NEVER_CONFIRM,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
  compactText,
  optionalStringInput,
  requiredStringInput,
  stringListInput,
  webMcpTextResult,
  taskChatId,
  withExecute,
} from "./shared";

const WEBMCP_CONVERSATION_RESULT_MAX_CHARS = 12_000;
const WEBMCP_CONVERSATION_LIST_RESULT_MAX_CHARS = 6_000;
const CONVERSATION_LIST_LIMIT = 8;
const CONVERSATION_MESSAGE_LIMIT = 6;
const CONVERSATION_MESSAGE_TEXT_LIMIT = 320;
const CONVERSATION_ARTIFACT_LIMIT = 4;
const RCP_SKILL_LIST_LIMIT = 4;
const PROVIDER_SKILL_LIST_LIMIT = 6;

function compactConversationMessage(message: ChatMessage): Record<string, unknown> {
  return {
    message_id: message.message_id,
    task_id: message.operation_id ?? null,
    role: message.role,
    mode: message.mode,
    trigger: message.trigger,
    timestamp: message.timestamp,
    text: compactText(message.text, CONVERSATION_MESSAGE_TEXT_LIMIT),
    text_truncated: message.text.length > CONVERSATION_MESSAGE_TEXT_LIMIT,
  };
}

function latestConversationTask(tasks: AgentTask[]): AgentTask | null {
  return (
    [...tasks]
      .sort(
        (left, right) =>
          Date.parse(left.created_at) - Date.parse(right.created_at) ||
          left.operation_id.localeCompare(right.operation_id),
      )
      .at(-1) ?? null
  );
}

function latestAssistantAnswer(
  transcript: ChatTranscript,
  latestTask: AgentTask | null,
): string | null {
  const matching = latestTask
    ? [...transcript.messages]
        .reverse()
        .find(
          (message) =>
            message.role === "assistant" && message.operation_id === latestTask.operation_id,
        )
    : null;
  const fallback = [...transcript.messages]
    .reverse()
    .find((message) => message.role === "assistant");
  const taskFallback = Array.isArray(latestTask?.result?.messages)
    ? latestTask.result.messages
        .filter(
          (message): message is string => typeof message === "string" && Boolean(message.trim()),
        )
        .at(-1)
    : null;
  const answer = matching?.text ?? taskFallback ?? fallback?.text;
  return answer ? compactText(answer, 1_000) : null;
}

function compactGraphUpdate(task: AgentTask | null): Record<string, unknown> | null {
  const update = task?.result?.graph_update;
  if (!update) return null;
  return {
    status: update.status,
    applied_revision: update.applied_revision,
    change_summary: update.change_summary.slice(0, 3).map((item) => compactText(item, 140)),
    proposal_ids: update.proposal_ids.slice(0, 8),
    validation_messages: update.validation_messages
      .slice(0, 3)
      .map((item) => compactText(item, 140)),
    correction_rounds: update.correction_rounds,
    repairable: update.repairable,
  };
}

function conversationRefusal(
  latestTask: AgentTask | null,
  relatedTasks: AgentTask[],
  taskStartPending: boolean,
  runTruthScope: string[],
  providerReady: boolean,
  projectGraphTarget?: GraphTargetRef,
  conversationGraphTarget?: GraphTargetRef,
): string | null {
  if (!sameGraphTarget(conversationGraphTarget ?? latestTask?.graph_target, projectGraphTarget)) {
    return "This Auto-research branch conversation is read-only.";
  }
  const active = relatedTasks.find((task) => task.active);
  if (active) return active.status_message || "A turn in this conversation is already active.";
  const paused = resumablePausedChatTask(relatedTasks);
  if (paused) return paused.status_message || "Resume or retry the paused turn before sending.";
  if (taskStartPending) return "Another task start is already being submitted.";
  if (!runTruthScope.length) return "This conversation has no repository scope.";
  if (!providerReady) return "The configured provider is not ready on its execution machine.";
  return null;
}

/** Backend reads a conversation tool needs beyond the page snapshot: the exact
 * transcript, and the exact task record behind a transcript message when that
 * task has aged out of the bounded task window the page holds. */
export type WebMcpConversationSource = {
  loadTranscript: (chatId: string) => Promise<ChatTranscript>;
  loadTask: (operationId: string) => Promise<AgentTask | null>;
};

async function resolveProjectConversationContext(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  chatId: string,
  source: WebMcpConversationSource,
  taskStartPending: boolean,
) {
  const transcript = await source.loadTranscript(chatId);
  if (transcript.chat_id !== chatId) {
    throw new Error(`Conversation ${chatId} returned a mismatched transcript.`);
  }
  const surface = transcript.kind;
  const node = transcript.node_id ? project.graph.nodes[transcript.node_id] : null;
  if (surface === "node_chat" && !node) {
    throw new Error(`Conversation ${chatId} points to a node outside the current project graph.`);
  }
  let relatedTasks = relatedChatTasks(tasks, surface, transcript.node_id, chatId);
  let latestTask = latestConversationTask(relatedTasks);
  if (!latestTask) {
    // The page task cache is a bounded recent window. A saved conversation whose
    // turns have aged out of it still carries its durable identity (graph
    // target, native session, scope) on the task behind its latest message, so
    // read that exact record before deciding whether Send may continue it.
    const lastOperationId =
      [...transcript.messages].reverse().find((message) => message.operation_id)?.operation_id ??
      null;
    const exact = lastOperationId ? await source.loadTask(lastOperationId) : null;
    if (exact && taskChatId(exact) === chatId) {
      latestTask = exact;
      relatedTasks = [exact];
    }
  }
  const profile = project.agent_profiles[surface];
  const fallbackConfig: AgentRunConfig = {
    provider: profile.provider,
    model: profile.model,
    reasoning: profile.reasoning,
    run_on: profile.run_on,
  };
  const config = latestPersistedChatConfig(transcript.messages, relatedTasks, fallbackConfig);
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const providerReady =
    readiness === undefined || Boolean(readiness.installed && readiness.authenticated);
  const runTruthScope =
    latestTask?.request.run_truth_scope ?? project.default_run_truth_scope ?? [];
  return {
    transcript,
    surface,
    node,
    relatedTasks,
    latestTask,
    profile,
    config,
    readiness,
    providerReady,
    runTruthScope,
    sessionId: resolvedChatSessionId(relatedTasks),
    refusal: conversationRefusal(
      latestTask,
      relatedTasks,
      taskStartPending,
      runTruthScope,
      providerReady,
      project.graph_target,
      transcript.graph_target,
    ),
  };
}

export async function inspectProjectConversation(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  input: Record<string, unknown>,
  source: WebMcpConversationSource,
  taskStartPending = false,
): Promise<Record<string, unknown>> {
  const chatId = requiredStringInput(input, "chat_id");
  const context = await resolveProjectConversationContext(
    project,
    tasks,
    chatId,
    source,
    taskStartPending,
  );
  const {
    transcript,
    surface,
    node,
    latestTask,
    profile,
    config,
    readiness,
    providerReady,
    runTruthScope,
    sessionId,
    refusal,
  } = context;
  const enabledCatalog = filterSkillCatalogToDefaults(
    project.skill_catalog ?? [],
    project.skill_defaults ?? { workflow_ids: [], skill_ids: [] },
  );
  const providerInventory =
    project.provider_skill_inventories?.[config.run_on]?.[config.provider] ?? null;
  const enabledProviderSkills = (providerInventory?.skills ?? []).filter((item) => item.enabled);
  const enabledWorkflows = enabledCatalog.filter((item) => item.kind === "workflow");
  const enabledSkills = enabledCatalog.filter((item) => item.kind === "skill");
  const recentMessages = transcript.messages.slice(-CONVERSATION_MESSAGE_LIMIT);
  return {
    project_id: project.id,
    chat_id: chatId,
    kind: surface,
    node_id: transcript.node_id,
    node_title: node ? compactText(node.title, 160) : null,
    title: compactText(transcript.title, 160),
    updated_at: transcript.updated_at,
    message_count: transcript.message_count,
    transcript_truncated: transcript.messages.length > recentMessages.length,
    recent_messages: recentMessages.map(compactConversationMessage),
    latest_task: latestTask
      ? {
          task_id: latestTask.operation_id,
          status: latestTask.status_label,
          status_message: compactText(latestTask.status_message, 180),
          active: latestTask.active,
          awaiting_human: latestTask.awaiting_human,
          paused: latestTask.paused,
          failed: latestTask.failed,
          settled: latestTask.settled,
          runtime_used: latestTask.runtime_id,
          final_answer: latestAssistantAnswer(transcript, latestTask),
          graph_update: compactGraphUpdate(latestTask),
          artifacts: taskArtifacts(latestTask)
            .slice(0, CONVERSATION_ARTIFACT_LIMIT)
            .map((artifact) => ({
              viewer_id: `task:${latestTask.operation_id}:${artifact.artifact_id}`,
              name: compactText(artifact.name, 96),
              media_type: artifact.media_type,
              view: artifact.view,
              can_download: artifact.can_download,
              available: artifact.available,
              can_open: artifact.can_open,
              kept_filename: artifact.kept_filename ?? null,
            })),
        }
      : null,
    send_options: {
      can_send: refusal === null,
      refusal_reason: refusal,
      modes: ["discuss", "work"],
      run_truth_scope: runTruthScope.slice(0, 8),
      run_truth_scope_truncated: runTruthScope.length > 8,
      stable_session_id: sessionId,
      provider: config.provider,
      provider_label: readiness?.label ?? config.provider,
      configured_runtime: profile.runtime,
      last_runtime_used: latestTask?.runtime_id ?? null,
      model: config.model || "provider-default",
      reasoning: config.reasoning,
      run_on: config.run_on,
      provider_ready: providerReady,
      workflows_total: enabledWorkflows.length,
      workflows_truncated: enabledWorkflows.length > RCP_SKILL_LIST_LIMIT,
      workflows: enabledWorkflows.slice(0, RCP_SKILL_LIST_LIMIT).map((item) => ({
        id: item.id,
        label: compactText(item.label, 80),
        description: compactText(item.description, 120),
      })),
      skills_total: enabledSkills.length,
      skills_truncated: enabledSkills.length > RCP_SKILL_LIST_LIMIT,
      skills: enabledSkills.slice(0, RCP_SKILL_LIST_LIMIT).map((item) => ({
        id: item.id,
        label: compactText(item.label, 80),
        description: compactText(item.description, 120),
      })),
      provider_skills: {
        status: providerInventory?.status ?? "unavailable",
        stale: providerInventory?.stale ?? false,
        diagnostic: providerInventory?.diagnostic
          ? compactText(providerInventory.diagnostic, 180)
          : null,
        total: enabledProviderSkills.length,
        truncated: enabledProviderSkills.length > PROVIDER_SKILL_LIST_LIMIT,
        items: enabledProviderSkills.slice(0, PROVIDER_SKILL_LIST_LIMIT).map((item) => ({
          name: compactText(item.name, 80),
          label: compactText(item.label, 80),
        })),
      },
    },
  };
}

export function listProjectConversations(
  project: ProjectSnapshot,
  summaries: ChatSummary[],
  summaryTotal: number,
  input: Record<string, unknown>,
): Record<string, unknown> {
  const nodeId = optionalStringInput(input, "node_id");
  if (nodeId && !project.graph.nodes[nodeId]) {
    throw new Error(`Node ${nodeId} is not present in the current project graph.`);
  }
  const query = optionalStringInput(input, "query")?.trim().toLowerCase() ?? null;
  const matches = summaries
    .filter((summary) => (nodeId ? summary.node_id === nodeId : true))
    .filter(
      (summary) =>
        !query ||
        [summary.title, summary.last_message_preview].some((value) =>
          value.toLowerCase().includes(query),
        ),
    )
    .sort(
      (left, right) =>
        Date.parse(right.updated_at) - Date.parse(left.updated_at) ||
        left.chat_id.localeCompare(right.chat_id),
    );
  const visible = matches.slice(0, CONVERSATION_LIST_LIMIT);
  return {
    project_id: project.id,
    total: Math.max(summaryTotal, summaries.length),
    loaded: summaries.length,
    matched: matches.length,
    returned: visible.length,
    truncated: matches.length > visible.length,
    conversations: visible.map((summary) => {
      const node = summary.node_id ? project.graph.nodes[summary.node_id] : null;
      return {
        chat_id: summary.chat_id,
        kind: summary.kind,
        node_id: summary.node_id,
        node_title: node ? compactText(node.title, 96) : null,
        title: compactText(summary.title, 96),
        updated_at: summary.updated_at,
        message_count: summary.message_count,
        last_message_preview: compactText(summary.last_message_preview, 160),
      };
    }),
  };
}

export const LIST_CONVERSATIONS_TOOL: WebMcpToolSpec = {
  name: "rcp_list_conversations",
  description:
    "List saved RCP conversations in the open project so an exact chat_id can be inspected or resumed.",
  inputSchema: {
    type: "object",
    properties: {
      node_id: {
        type: "string",
        minLength: 1,
        description: "Optional exact graph node id whose conversations should be listed.",
      },
      query: {
        type: "string",
        minLength: 1,
        description: "Optional words from the conversation title or latest message.",
      },
    },
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export const INSPECT_CONVERSATION_TOOL: WebMcpToolSpec = {
  name: "rcp_inspect_conversation",
  description: "Read one bounded RCP conversation and its current Send options.",
  inputSchema: {
    type: "object",
    properties: {
      chat_id: {
        type: "string",
        minLength: 1,
        description: "Exact current RCP conversation id.",
      },
    },
    required: ["chat_id"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export function projectConversationToolDefinitions(
  project: ProjectSnapshot,
  summaries: ChatSummary[],
  summaryTotal: number,
  tasks: AgentTask[],
  source: WebMcpConversationSource,
  taskStartPending = false,
): WebMcpToolDefinition[] {
  return [
    withExecute(LIST_CONVERSATIONS_TOOL, (input) =>
      webMcpTextResult(
        listProjectConversations(project, summaries, summaryTotal, input),
        WEBMCP_CONVERSATION_LIST_RESULT_MAX_CHARS,
      ),
    ),
    withExecute(INSPECT_CONVERSATION_TOOL, async (input) =>
      webMcpTextResult(
        await inspectProjectConversation(project, tasks, input, source, taskStartPending),
        WEBMCP_CONVERSATION_RESULT_MAX_CHARS,
      ),
    ),
  ];
}

type CreateWebMcpConversation = (kind: ChatKind, node: GraphNode | null) => string;
type StartWebMcpConversationTurn = (
  submission: ConversationTurnSubmission,
  requestId?: string,
) => Promise<AgentTask>;

function exactEnabledSkillIds(
  project: ProjectSnapshot,
  kind: "workflow" | "skill",
  requested: string[],
): string[] {
  const enabled = new Set(
    filterSkillCatalogToDefaults(
      project.skill_catalog ?? [],
      project.skill_defaults ?? { workflow_ids: [], skill_ids: [] },
    )
      .filter((item) => item.kind === kind)
      .map((item) => item.id),
  );
  const unknown = requested.filter((id) => !enabled.has(id));
  if (unknown.length) {
    throw new Error(`Unknown or disabled RCP ${kind} ids: ${unknown.join(", ")}.`);
  }
  return requested;
}

function exactEnabledProviderSkillNames(
  project: ProjectSnapshot,
  config: AgentRunConfig,
  requested: string[],
): string[] {
  const inventory = project.provider_skill_inventories?.[config.run_on]?.[config.provider];
  const enabled = new Set(
    (inventory?.skills ?? []).filter((item) => item.enabled).map((item) => item.name),
  );
  const unknown = requested.filter((name) => !enabled.has(name));
  if (unknown.length) {
    throw new Error(`Unknown or disabled provider skill names: ${unknown.join(", ")}.`);
  }
  return requested;
}

/** The conversation, node, and provider profile one Send would use, without starting it. */
export async function conversationSendTarget(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  input: Record<string, unknown>,
  source: WebMcpConversationSource,
  taskStartPending = false,
) {
  const requestedChatId = optionalStringInput(input, "chat_id");
  const requestedNodeId = optionalStringInput(input, "node_id");
  if (requestedChatId && requestedNodeId) {
    throw new Error("chat_id and node_id cannot be supplied together.");
  }
  const existing = requestedChatId
    ? await resolveProjectConversationContext(
        project,
        tasks,
        requestedChatId,
        source,
        taskStartPending,
      )
    : null;
  const node = existing
    ? existing.node
    : requestedNodeId
      ? project.graph.nodes[requestedNodeId]
      : null;
  if (!existing && requestedNodeId && !node) {
    throw new Error(`Node ${requestedNodeId} is not present in the current project graph.`);
  }
  const surface: ChatKind = existing?.surface ?? (node ? "node_chat" : "project_chat");
  const profile = existing?.profile ?? project.agent_profiles[surface];
  const config: AgentRunConfig = existing?.config ?? {
    provider: profile.provider,
    model: profile.model,
    reasoning: profile.reasoning,
    run_on: profile.run_on,
  };
  const runTruthScope = existing?.runTruthScope ?? project.default_run_truth_scope ?? [];
  const readiness = project.provider_readiness[config.run_on]?.[config.provider];
  const providerReady =
    readiness === undefined || Boolean(readiness.installed && readiness.authenticated);
  // Why a Send here would be refused, known before any confirmation card is shown.
  const refusal =
    existing?.refusal ??
    conversationRefusal(null, [], taskStartPending, runTruthScope, providerReady);
  return { existing, node, surface, config, runTruthScope, refusal };
}

export async function sendProjectConversationMessage(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  input: Record<string, unknown>,
  source: WebMcpConversationSource,
  taskStartPending: boolean,
  createConversation: CreateWebMcpConversation,
  startTurn: StartWebMcpConversationTurn,
  requestId?: string,
): Promise<Record<string, unknown>> {
  if (Object.prototype.hasOwnProperty.call(input, "references")) {
    throw new Error("Project references are not supported by WebMCP Send.");
  }
  const message = requiredStringInput(input, "message").trim();
  if (message.length > 2_000) throw new Error("message must contain at most 2000 characters.");
  const mode = requiredStringInput(input, "mode");
  if (mode !== "discuss" && mode !== "work") {
    throw new Error("mode must be discuss or work.");
  }
  const workflowIds = exactEnabledSkillIds(
    project,
    "workflow",
    stringListInput(input, "workflow_ids"),
  );
  const skillIds = exactEnabledSkillIds(project, "skill", stringListInput(input, "skill_ids"));
  const { existing, node, surface, config, runTruthScope, refusal } = await conversationSendTarget(
    project,
    tasks,
    input,
    source,
    taskStartPending,
  );
  if (refusal) throw new Error(refusal);
  const providerSkillNames = exactEnabledProviderSkillNames(
    project,
    config,
    stringListInput(input, "provider_skill_names"),
  );
  const chatId = existing?.transcript.chat_id ?? createConversation(surface, node);
  const task = await startTurn(
    {
      kind: surface,
      config,
      runTruthScope,
      nodeId: node?.id ?? null,
      message,
      chatId,
      sessionId: existing?.sessionId ?? null,
      mode,
      skills: { workflow_ids: workflowIds, skill_ids: skillIds },
      providerSkillNames,
    },
    requestId,
  );
  return {
    project_id: project.id,
    chat_id: chatId,
    task_id: task.operation_id,
    kind: surface,
    node_id: node?.id ?? null,
    mode,
    accepted: true,
    status: task.status_label,
    active: task.active,
    queued: task.queued,
  };
}

export const SEND_CONVERSATION_TOOL: WebMcpToolSpec = {
  name: "rcp_send_conversation_message",
  description: "Start one asynchronous RCP Discuss or Work turn in a new or existing conversation.",
  inputSchema: {
    type: "object",
    properties: {
      message: {
        type: "string",
        minLength: 1,
        maxLength: 2_000,
        description: "Natural-language request for the provider turn.",
      },
      mode: {
        type: "string",
        enum: ["discuss", "work"],
        description: "Discuss cannot change project truth; Work uses RCP's bounded Work authority.",
      },
      chat_id: {
        type: "string",
        minLength: 1,
        description:
          "Existing conversation to resume; omit for a fresh project or node conversation.",
      },
      node_id: {
        type: "string",
        minLength: 1,
        description: "Current node for a fresh node conversation; omit when chat_id is supplied.",
      },
      workflow_ids: {
        type: "array",
        items: { type: "string", minLength: 1 },
        maxItems: 32,
        description: "Exact enabled workflow ids returned by conversation inspection.",
      },
      skill_ids: {
        type: "array",
        items: { type: "string", minLength: 1 },
        maxItems: 32,
        description: "Exact enabled RCP skill ids returned by conversation inspection.",
      },
      provider_skill_names: {
        type: "array",
        items: { type: "string", minLength: 1 },
        maxItems: 32,
        description: "Exact provider-native skill names returned by conversation inspection.",
      },
    },
    required: ["message", "mode"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false },
  // Work can change project truth; Discuss cannot.
  confirm: (input) => input.mode === "work",
};

export function projectConversationSendToolDefinitions(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  source: WebMcpConversationSource,
  taskStartPending: boolean,
  createConversation: CreateWebMcpConversation,
  startTurn: StartWebMcpConversationTurn,
): WebMcpToolDefinition[] {
  return [
    withExecute(SEND_CONVERSATION_TOOL, async (toolInput, requestId) =>
      webMcpTextResult(
        await sendProjectConversationMessage(
          project,
          tasks,
          toolInput,
          source,
          taskStartPending,
          createConversation,
          startTurn,
          requestId,
        ),
      ),
    ),
  ];
}
