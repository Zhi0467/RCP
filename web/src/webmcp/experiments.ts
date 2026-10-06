import { sameGraphTarget } from "../core/graphTarget";
import { isControlNode } from "../graph/researchType";
import type {
  AgentTask,
  Episode,
  GraphNode,
  GraphTargetRef,
  ProjectSnapshot,
  WatcherRecord,
} from "../core/types";
import {
  ALWAYS_CONFIRM,
  NEVER_CONFIRM,
  type WebMcpJsonSchema,
  type WebMcpToolDefinition,
  type WebMcpToolSpec,
  compactExperimentControl,
  compactNode,
  compactText,
  optionalStringInput,
  requiredStringInput,
  webMcpTextResult,
  withExecute,
} from "./shared";

const WEBMCP_EXPERIMENT_RESULT_MAX_CHARS = 12_000;

function exactExperiment(project: ProjectSnapshot, input: Record<string, unknown>): GraphNode {
  const experimentId = requiredStringInput(input, "experiment_id");
  const node = project.graph.nodes[experimentId];
  if (!node || !isControlNode(node.type)) {
    throw new Error(`Experiment ${experimentId} is not present in the current project graph.`);
  }
  return node;
}

function experimentTasks(tasks: AgentTask[], experimentId: string): AgentTask[] {
  return tasks
    .filter(
      (task) =>
        task.request.control_node_id === experimentId ||
        (task.kind === "node_chat" &&
          task.request.node_id === experimentId &&
          task.request.patch_kind === "experiment_loop"),
    )
    .sort(
      (left, right) =>
        Date.parse(right.created_at) - Date.parse(left.created_at) ||
        right.operation_id.localeCompare(left.operation_id),
    );
}

function experimentWatchers(
  watchers: WatcherRecord[],
  experimentId: string,
  target: GraphTargetRef,
): WatcherRecord[] {
  return watchers
    .filter(
      (watcher) =>
        watcher.continuation.control_node_id === experimentId &&
        sameGraphTarget(watcher.graph_target, target),
    )
    .sort(
      (left, right) =>
        Date.parse(right.created_at) - Date.parse(left.created_at) ||
        right.watcher_id.localeCompare(left.watcher_id),
    );
}

function compactExperimentNode(node: GraphNode): Record<string, unknown> {
  const attempts = node.attempts ?? [];
  const latestAttempt = attempts.at(-1) ?? null;
  return {
    ...compactNode(node),
    current_summary: node.current_summary ? compactText(node.current_summary, 280) : null,
    next_action: node.next_action ? compactText(node.next_action, 220) : null,
    completion_criteria: (node.completion_criteria ?? [])
      .slice(0, 4)
      .map((criterion) => compactText(criterion, 140)),
    attempt_count: attempts.length,
    latest_attempt: latestAttempt
      ? {
          id: latestAttempt.id,
          sequence: latestAttempt.sequence,
          purpose: compactText(latestAttempt.purpose, 140),
          status: latestAttempt.status,
          outcome: latestAttempt.outcome ? compactText(latestAttempt.outcome, 240) : null,
          failure_reason: latestAttempt.failure_reason
            ? compactText(latestAttempt.failure_reason, 180)
            : null,
          job_refs: latestAttempt.job_refs.slice(0, 4),
        }
      : null,
  };
}

function pageStartRefusal(
  taskStartPending: boolean,
  mutationsDisabled: boolean,
  startRequiresSync: boolean,
): string | null {
  if (mutationsDisabled) return "Graph mutations are currently disabled.";
  if (startRequiresSync) return "Sync staged graph changes before starting an episode.";
  if (taskStartPending) return "Another task start is already being submitted.";
  return null;
}

/** Why no Experiment in the open project can start now, or null when one can. */
export function experimentStartRefusal(
  project: ProjectSnapshot,
  taskStartPending: boolean,
  mutationsDisabled: boolean,
  startRequiresSync: boolean,
): string | null {
  return (
    pageStartRefusal(taskStartPending, mutationsDisabled, startRequiresSync) ??
    (Object.values(project.experiment_control).some((control) => control.can_start)
      ? null
      : "No Experiment in this project can start now.")
  );
}

export function inspectProjectExperiment(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  watchers: WatcherRecord[],
  input: Record<string, unknown>,
  taskStartPending = false,
  mutationsDisabled = false,
  startRequiresSync = false,
): Record<string, unknown> {
  const node = exactExperiment(project, input);
  const control = project.experiment_control[node.id];
  if (!control) throw new Error(`Experiment ${node.id} has no current control projection.`);
  const relatedTasks = experimentTasks(tasks, node.id);
  const relatedWatchers = experimentWatchers(watchers, node.id, project.graph_target);
  const startRefusal = pageStartRefusal(taskStartPending, mutationsDisabled, startRequiresSync);
  return {
    project_id: project.id,
    graph_revision: project.graph.revision,
    experiment: compactExperimentNode(node),
    control: compactExperimentControl(control),
    page_start_refusal: startRefusal,
    start_available: control.can_start && startRefusal === null,
    tasks: relatedTasks.slice(0, 3).map((task) => ({
      task_id: task.operation_id,
      episode_id: task.episode_id ?? task.request.control_episode_id ?? null,
      invocation: task.request.control_invocation ?? null,
      status: task.status_label,
      status_message: compactText(task.status_message, 160),
      active: task.active,
      awaiting_human: task.awaiting_human,
      settled: task.settled,
      runtime_used: task.runtime_id,
    })),
    watchers: relatedWatchers.slice(0, 4).map((watcher) => ({
      watcher_id: watcher.watcher_id,
      episode_id: watcher.episode_id,
      status: watcher.status,
      kind: "check_command" in watcher ? "external" : "graph",
      next_check_at: "next_check_at" in watcher ? watcher.next_check_at : null,
      stop_reason: watcher.stop_reason ? compactText(watcher.stop_reason, 160) : null,
      last_error:
        "last_error" in watcher && watcher.last_error ? compactText(watcher.last_error, 180) : null,
    })),
    report_viewer_id: control.can_open_report ? `report:${control.report_episode_id}` : null,
  };
}

type StartWebMcpExperiment = (node: GraphNode, invocationCeiling?: number) => Promise<AgentTask>;

/** A confirmed voice start passes `invocation_ceiling`; it must still be the node's own. */
export async function startProjectExperiment(
  project: ProjectSnapshot,
  input: Record<string, unknown>,
  startExperiment: StartWebMcpExperiment,
): Promise<Record<string, unknown>> {
  const node = exactExperiment(project, input);
  const ceiling = input.invocation_ceiling;
  if (ceiling !== undefined && ceiling !== node.invocation_ceiling) {
    throw new Error(`Experiment ${node.id}'s invocation ceiling changed; nothing started.`);
  }
  const task = await startExperiment(node, ceiling as number | undefined);
  return {
    project_id: project.id,
    experiment_id: node.id,
    task_id: task.operation_id,
    episode_id: task.episode_id ?? task.request.control_episode_id ?? null,
    accepted: true,
    status: task.status_label,
    active: task.active,
    queued: task.queued,
  };
}

const EXPERIMENT_INPUT_SCHEMA: WebMcpJsonSchema = {
  type: "object",
  properties: {
    experiment_id: {
      type: "string",
      minLength: 1,
      description: "Exact current Experiment node id returned by an RCP read tool.",
    },
  },
  required: ["experiment_id"],
  additionalProperties: false,
};

export const INSPECT_EXPERIMENT_TOOL: WebMcpToolSpec = {
  name: "rcp_inspect_experiment",
  description: "Read one RCP Experiment's current control, work, watchers, and report state.",
  inputSchema: EXPERIMENT_INPUT_SCHEMA,
  annotations: { readOnlyHint: true, untrustedContentHint: true },
  confirm: NEVER_CONFIRM,
};

export const START_EXPERIMENT_TOOL: WebMcpToolSpec = {
  name: "rcp_start_experiment",
  description: "Start the next bounded episode for one exact RCP Experiment.",
  inputSchema: EXPERIMENT_INPUT_SCHEMA,
  annotations: { readOnlyHint: false },
  confirm: ALWAYS_CONFIRM,
};

export function projectExperimentToolDefinitions(
  project: ProjectSnapshot,
  tasks: AgentTask[],
  watchers: WatcherRecord[],
  taskStartPending: boolean,
  startReturning: boolean,
  mutationsDisabled: boolean,
  startRequiresSync: boolean,
  startExperiment: StartWebMcpExperiment,
): WebMcpToolDefinition[] {
  const inspectTool = withExecute(INSPECT_EXPERIMENT_TOOL, (toolInput) =>
    webMcpTextResult(
      inspectProjectExperiment(
        project,
        tasks,
        watchers,
        toolInput,
        taskStartPending,
        mutationsDisabled,
        startRequiresSync,
      ),
      WEBMCP_EXPERIMENT_RESULT_MAX_CHARS,
    ),
  );
  const startIsDiscoverable =
    startReturning ||
    experimentStartRefusal(project, taskStartPending, mutationsDisabled, startRequiresSync) ===
      null;
  if (!startIsDiscoverable) return [inspectTool];
  return [
    inspectTool,
    withExecute(START_EXPERIMENT_TOOL, async (toolInput) =>
      webMcpTextResult(await startProjectExperiment(project, toolInput, startExperiment)),
    ),
  ];
}

type StopWebMcpExperiment = (experimentId: string, episodeId: string) => Promise<void>;

export async function stopProjectExperimentEpisode(
  project: ProjectSnapshot,
  input: Record<string, unknown>,
  stopExperiment: StopWebMcpExperiment,
): Promise<Record<string, unknown>> {
  const node = exactExperiment(project, input);
  const episodeId = requiredStringInput(input, "episode_id");
  const control = project.experiment_control[node.id];
  if (!control) throw new Error(`Experiment ${node.id} has no current control projection.`);
  if (control.episode_id !== episodeId) {
    throw new Error(`Episode ${episodeId} is not the live episode for Experiment ${node.id}.`);
  }
  if (!control.can_stop) {
    throw new Error(control.reasons.join(" ") || `Experiment ${node.id} cannot be stopped now.`);
  }
  await stopExperiment(node.id, episodeId);
  return {
    project_id: project.id,
    experiment_id: node.id,
    episode_id: episodeId,
    stop_requested: true,
    graceful: true,
  };
}

type StopWebMcpAutoResearch = (episodeId: string) => Promise<void>;

export async function stopProjectAutoResearchEpisode(
  project: ProjectSnapshot,
  episodes: Episode[],
  input: Record<string, unknown>,
  stopAutoResearch: StopWebMcpAutoResearch,
): Promise<Record<string, unknown>> {
  const episodeId = requiredStringInput(input, "episode_id");
  const episode = episodes.find(
    (candidate) => candidate.episode_id === episodeId && candidate.mode === "auto_research",
  );
  if (!episode) {
    throw new Error(
      `Episode ${episodeId} is not a current Auto-research episode; an Experiment episode needs experiment_id.`,
    );
  }
  if (!episode.can_stop) {
    throw new Error(`Auto-research episode ${episodeId} cannot be stopped now.`);
  }
  await stopAutoResearch(episodeId);
  return {
    project_id: project.id,
    episode_id: episodeId,
    stop_requested: true,
    graceful: true,
  };
}

/** Why no Experiment or Auto-research episode in the open project can stop now. */
export function episodeStopRefusal(project: ProjectSnapshot, episodes: Episode[]): string | null {
  const canStop =
    Object.values(project.experiment_control).some((control) => control.can_stop) ||
    episodes.some((episode) => episode.mode === "auto_research" && episode.can_stop);
  return canStop ? null : "No Experiment or Auto-research episode can stop now.";
}

export const STOP_EPISODE_TOOL: WebMcpToolSpec = {
  name: "rcp_stop_episode",
  description:
    "Request RCP's graceful Stop fence for one exact live Experiment or Auto-research episode.",
  inputSchema: {
    type: "object",
    properties: {
      episode_id: {
        type: "string",
        minLength: 1,
        description: "Exact live episode id returned by an RCP read or start tool.",
      },
      experiment_id: {
        type: "string",
        minLength: 1,
        description: "Exact Experiment node id for an Experiment episode; omit for Auto-research.",
      },
    },
    required: ["episode_id"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false },
  confirm: NEVER_CONFIRM,
};

export function projectEpisodeStopToolDefinitions(
  project: ProjectSnapshot,
  episodes: Episode[],
  stopExperiment: StopWebMcpExperiment,
  stopAutoResearch: StopWebMcpAutoResearch,
  stopPending = false,
): WebMcpToolDefinition[] {
  if (!stopPending && episodeStopRefusal(project, episodes)) return [];
  return [
    withExecute(STOP_EPISODE_TOOL, async (toolInput) =>
      webMcpTextResult(
        toolInput.experiment_id === undefined
          ? await stopProjectAutoResearchEpisode(project, episodes, toolInput, stopAutoResearch)
          : await stopProjectExperimentEpisode(project, toolInput, stopExperiment),
      ),
    ),
  ];
}

type StartWebMcpAutoResearch = (
  invocationCeiling: number,
  startingInstruction: string | null,
  codeWorktree: boolean,
) => Promise<Episode>;

export async function authorizeProjectAutoResearch(
  project: ProjectSnapshot,
  input: Record<string, unknown>,
  startAutoResearch: StartWebMcpAutoResearch,
): Promise<Record<string, unknown>> {
  const invocationCeiling = input.invocation_ceiling;
  if (
    typeof invocationCeiling !== "number" ||
    !Number.isSafeInteger(invocationCeiling) ||
    invocationCeiling < 1
  ) {
    throw new Error("invocation_ceiling must be an integer of at least 1.");
  }
  const startingInstruction = optionalStringInput(input, "starting_instruction")?.trim() ?? null;
  const codeWorktree = input.code_worktree ?? true;
  if (typeof codeWorktree !== "boolean") {
    throw new Error("code_worktree must be a boolean when supplied.");
  }
  const episode = await startAutoResearch(invocationCeiling, startingInstruction, codeWorktree);
  return {
    project_id: project.id,
    episode_id: episode.episode_id,
    accepted: true,
    status: episode.status,
    live: episode.live,
    invocation_ceiling: invocationCeiling,
  };
}

export const AUTHORIZE_AUTO_RESEARCH_TOOL: WebMcpToolSpec = {
  name: "rcp_authorize_auto_research",
  description:
    "Authorize one Auto-research episode for the open project, like the visible Auto-research form. It works on its own graph branch and returns the episode id.",
  inputSchema: {
    type: "object",
    properties: {
      invocation_ceiling: {
        type: "integer",
        minimum: 1,
        description: "Operational invocation ceiling: the most orchestrator turns it may use.",
      },
      starting_instruction: {
        type: "string",
        minLength: 1,
        description: "Optional starting instruction for the orchestrator.",
      },
      code_worktree: {
        type: "boolean",
        description: "False opts out of a code worktree; omitted or true leaves it to eligibility.",
      },
    },
    required: ["invocation_ceiling"],
    additionalProperties: false,
  },
  annotations: { readOnlyHint: false },
  confirm: ALWAYS_CONFIRM,
};

/** `refusal` is the same check that disables the visible Auto-research button. */
export function projectAutoResearchToolDefinitions(
  project: ProjectSnapshot,
  refusal: string | null,
  startAutoResearch: StartWebMcpAutoResearch,
): WebMcpToolDefinition[] {
  if (refusal) return [];
  return [
    withExecute(AUTHORIZE_AUTO_RESEARCH_TOOL, async (toolInput) =>
      webMcpTextResult(await authorizeProjectAutoResearch(project, toolInput, startAutoResearch)),
    ),
  ];
}
