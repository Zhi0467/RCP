import type {
  AgentTask,
  AgentTaskStatus,
  ExperimentControlState,
  GraphNode,
  WatcherRecord,
} from "./types";

export interface AgentTaskGroup {
  rootId: string;
  root: AgentTask;
  latest: AgentTask;
  attempts: AgentTask[];
}

export interface RunTaskProjection {
  actionable: AgentTaskGroup[];
  running: AgentTaskGroup[];
  completed: AgentTaskGroup[];
}

export type RunSectionKey = "running" | "actionable" | "completed";

export type ExperimentLoopHealth =
  | "starting"
  | "agent_active"
  | "waiting_on_watchers"
  | "degraded"
  | "stopping"
  | "human_stopped"
  | "paused_at_limit"
  | "needs_action"
  | "completed";

export interface ExperimentRun {
  node: GraphNode;
  control: ExperimentControlState | null;
  taskGroup: AgentTaskGroup | null;
  currentTask: AgentTask | null;
  watchers: WatcherRecord[];
  watcherItems: ExperimentWatcherItem[];
  currentWatchers: WatcherRecord[];
  health: ExperimentLoopHealth;
}

export type ExperimentRecommendedStep =
  | "wait"
  | "resume"
  | "retry"
  | "keep_loop"
  | "start_episode"
  | "stop_and_restart"
  | "resolve_requirements"
  | "review"
  | "none";

export interface ExperimentRecommendation {
  step: ExperimentRecommendedStep;
  label: string;
}

export interface ExperimentWatcherCounts {
  finished: number;
  degraded: number;
  running: number;
  stopped: number;
}

export interface ExperimentWatcherGroup {
  groupId: string;
  label: string;
  watchers: WatcherRecord[];
  counts: ExperimentWatcherCounts;
}

export type ExperimentWatcherItem =
  { kind: "group"; group: ExperimentWatcherGroup } | { kind: "watcher"; watcher: WatcherRecord };

export function watcherIsActive(watcher: WatcherRecord): boolean {
  return watcher.status === "active" || watcher.status === "degraded";
}

/**
 * Chats show the resources they can observe: an Experiment node's loop watchers and the exact
 * conversation's own self-wake watchers. Experiment-loop provenance does not make a chat its
 * owner, while generic self-wake watchers never leak into another conversation.
 */
export function visibleChatWatchers(
  watchers: WatcherRecord[],
  chatId: string,
  node: GraphNode | null | undefined,
): WatcherRecord[] {
  const experimentNodeId = node?.type === "experiment" ? node.id : null;
  const visible = new Map<string, WatcherRecord>();
  for (const watcher of watchers) {
    if (!watcherIsActive(watcher)) continue;
    const nodeLoopWatcher =
      experimentNodeId !== null &&
      watcher.continuation.patch_kind === "experiment_loop" &&
      watcher.continuation.control_node_id === experimentNodeId;
    const chatSelfWakeWatcher =
      watcher.chat_id === chatId && watcher.continuation.patch_kind === "work";
    if (nodeLoopWatcher || chatSelfWakeWatcher) visible.set(watcher.watcher_id, watcher);
  }
  return [...visible.values()];
}

export type RunEntry =
  | { kind: "task"; id: string; observedAt: string | null; group: AgentTaskGroup }
  | { kind: "experiment"; id: string; observedAt: string | null; experiment: ExperimentRun }
  | { kind: "blocker"; id: string; observedAt: string | null; node: GraphNode };

export interface RunProjection {
  running: RunEntry[];
  actionable: RunEntry[];
  completed: RunEntry[];
}

export interface RunProjectionInput {
  nodes: GraphNode[];
  tasks: AgentTask[];
  watchers?: WatcherRecord[];
  experimentControl?: Record<string, ExperimentControlState>;
  dismissedTaskIds?: ReadonlySet<string>;
}

const actionableStatuses = new Set(["failed", "paused", "interrupted"]);
const runningStatuses = new Set(["queued", "running", "pausing"]);
const terminalExperimentStatuses = new Set(["completed", "abandoned", "superseded"]);
const nonGatingOperationalReasons = new Set([
  "An experiment loop is already active.",
  "A graceful stop is finishing the current loop turn.",
  "Detached Experiment work is still running.",
]);

const healthSections: Record<ExperimentLoopHealth, RunSectionKey> = {
  starting: "running",
  agent_active: "running",
  waiting_on_watchers: "running",
  degraded: "running",
  stopping: "running",
  human_stopped: "actionable",
  paused_at_limit: "actionable",
  needs_action: "actionable",
  completed: "completed",
};

export function buildRunTaskProjection(
  tasks: AgentTask[],
  dismissedTaskIds: ReadonlySet<string> = new Set(),
): RunTaskProjection {
  const groups = groupAgentTasks(tasks).filter(
    (group) => !isTaskNotificationSuperseded(group.latest, tasks),
  );
  return {
    actionable: groups.filter(
      (group) =>
        actionableStatuses.has(group.latest.status) &&
        !dismissedTaskIds.has(group.latest.operation_id),
    ),
    running: groups.filter((group) => runningStatuses.has(group.latest.status)),
    completed: groups.filter((group) => group.latest.status === "succeeded"),
  };
}

export function isExperimentLoopTask(task: AgentTask): boolean {
  return (
    task.request?.patch_kind === "experiment_loop" && Boolean(task.request?.control_node_id ?? null)
  );
}

export function experimentRunSection(
  health: ExperimentLoopHealth,
  taskStatus: AgentTaskStatus | null = null,
): RunSectionKey {
  if (health === "stopping" && taskStatus && actionableStatuses.has(taskStatus)) {
    return "actionable";
  }
  return healthSections[health];
}

/**
 * Loop health reads the current task, the control state, the durable stop request, and the
 * Experiment's own watchers. The Experiment node's semantic `status` only decides the outcome
 * once no operational state applies.
 */
export function deriveExperimentLoopHealth(
  node: GraphNode,
  control: ExperimentControlState | null,
  taskStatus: AgentTaskStatus | null,
  currentWatchers: WatcherRecord[],
): ExperimentLoopHealth {
  const operational = control?.operational;
  const stopRequested = Boolean(operational?.stop_requested);
  if (stopRequested && !operational?.stop_settled) return "stopping";
  if (
    stopRequested &&
    operational?.stop_settled &&
    taskStatus &&
    actionableStatuses.has(taskStatus)
  ) {
    return "human_stopped";
  }
  if (taskStatus === "queued") return "starting";
  if (taskStatus === "running" || taskStatus === "pausing") return "agent_active";
  if (taskStatus && actionableStatuses.has(taskStatus)) return "needs_action";
  if (operational?.task_active) return "needs_action";

  const used = control?.invocations_used ?? 0;
  const remaining = control?.invocations_remaining ?? 0;
  const completionPending = Boolean(
    operational?.watcher_completion_pending ||
    currentWatchers.some((watcher) => watcher.status === "completed" && !watcher.notified),
  );
  const detachedWorkActive = Boolean(
    operational?.detached_work_active || currentWatchers.some(watcherIsActive),
  );
  const hasGraphGate = Boolean(
    control?.reasons.some((reason) => !nonGatingOperationalReasons.has(reason)),
  );
  const canWake = Boolean(
    !stopRequested &&
    remaining > 0 &&
    !operational?.episode_exited &&
    !operational?.session.diagnostic &&
    !hasGraphGate,
  );
  if ((completionPending || detachedWorkActive) && remaining <= 0) return "paused_at_limit";
  if (completionPending && !canWake) return "needs_action";
  if (detachedWorkActive && !canWake) return "needs_action";
  if (
    operational?.watcher_degraded ||
    currentWatchers.some((watcher) => watcher.status === "degraded")
  ) {
    return "degraded";
  }
  if (completionPending || detachedWorkActive) return "waiting_on_watchers";
  if (terminalExperimentStatuses.has(String(node.status ?? ""))) return "completed";
  if (stopRequested) return "human_stopped";
  if (remaining <= 0 && used > 0) return "paused_at_limit";
  return "needs_action";
}

/** Human guidance derived only from canonical task, control, and watcher state. */
export function experimentRecommendation(run: ExperimentRun): ExperimentRecommendation {
  const task = run.currentTask;
  const operational = run.control?.operational;
  if (task && runningStatuses.has(task.status)) {
    return { step: "wait", label: "Wait for the agent" };
  }
  if (run.health === "stopping") {
    return { step: "wait", label: "Wait for the current turn to finish" };
  }
  if (task?.can_resume && (task.status === "paused" || task.status === "interrupted")) {
    return {
      step: "resume",
      label: task.can_retry ? "Resume this episode, or switch provider" : "Resume this episode",
    };
  }
  if (task?.can_retry) {
    return { step: "retry", label: "Retry this episode, or switch provider" };
  }
  if (operational?.session.diagnostic && run.control?.episode_id) {
    return { step: "stop_and_restart", label: "Stop loop, then start a new episode" };
  }
  if (run.health === "paused_at_limit") {
    return { step: "start_episode", label: "Start a new episode" };
  }
  if (run.health === "degraded") {
    return { step: "keep_loop", label: "Keep loop running; check now if needed" };
  }
  if (run.health === "waiting_on_watchers") {
    return { step: "wait", label: "Wait for watcher completion" };
  }
  if (run.health === "human_stopped") {
    return { step: "start_episode", label: "Start a new episode" };
  }
  if (run.health === "completed") {
    return { step: "none", label: "No action needed" };
  }
  if ((run.control?.reasons.length ?? 0) > 0) {
    return { step: "resolve_requirements", label: "Resolve the run requirements" };
  }
  if (!run.control?.episode_id) {
    return { step: "start_episode", label: "Start an episode" };
  }
  if (task && actionableStatuses.has(task.status)) {
    return { step: "stop_and_restart", label: "Stop loop, then start a new episode" };
  }
  return { step: "review", label: "Review the loop state" };
}

export function buildExperimentRun(
  node: GraphNode,
  control: ExperimentControlState | null,
  tasks: AgentTask[],
  allWatchers: WatcherRecord[],
): ExperimentRun {
  const watchers = allWatchers
    .filter(
      (watcher) =>
        watcher.continuation.patch_kind === "experiment_loop" &&
        watcher.continuation.control_node_id === node.id,
    )
    .sort(
      (left, right) =>
        right.created_at.localeCompare(left.created_at) ||
        left.watcher_id.localeCompare(right.watcher_id),
    );
  const currentWatchers = watchers.filter(
    (watcher) =>
      Boolean(control?.episode_id) &&
      watcher.continuation.control_episode_id === control?.episode_id,
  );
  const { taskGroup, currentTask } = currentExperimentTaskGroup(node.id, control, tasks);
  const taskStatus =
    currentTask?.status ?? asAgentTaskStatus(control?.operational?.current_status ?? null);
  return {
    node,
    control,
    taskGroup,
    currentTask,
    watchers,
    watcherItems: experimentWatcherDisplayItems(watchers),
    currentWatchers,
    health: deriveExperimentLoopHealth(node, control, taskStatus, currentWatchers),
  };
}

/** Keep an Experiment's immutable watcher group visible as one operational unit. */
export function experimentWatcherDisplayItems(watchers: WatcherRecord[]): ExperimentWatcherItem[] {
  const groups = new Map<string, ExperimentWatcherGroup>();
  const items: ExperimentWatcherItem[] = [];
  for (const watcher of watchers) {
    if (!watcher.group_id) {
      items.push({ kind: "watcher", watcher });
      continue;
    }
    let group = groups.get(watcher.group_id);
    if (!group) {
      group = {
        groupId: watcher.group_id,
        label: watcher.group_label ?? watcher.group_id,
        watchers: [],
        counts: { finished: 0, degraded: 0, running: 0, stopped: 0 },
      };
      groups.set(watcher.group_id, group);
      items.push({ kind: "group", group });
    }
    group.watchers.push(watcher);
    group.counts[watcherGroupCountKey(watcher)] += 1;
  }
  return items;
}

function watcherGroupCountKey(watcher: WatcherRecord): keyof ExperimentWatcherCounts {
  switch (watcher.status) {
    case "completed":
      return "finished";
    case "active":
      return "running";
    case "degraded":
      return "degraded";
    case "stopped":
      return "stopped";
  }
}

export function buildRunProjection(input: RunProjectionInput): RunProjection {
  const watchers = input.watchers ?? [];
  const experimentControl = input.experimentControl ?? {};
  const ingestion = buildRunTaskProjection(
    input.tasks.filter((task) => task.kind === "seed" || task.kind === "refresh"),
    input.dismissedTaskIds ?? new Set<string>(),
  );
  const sections: Record<RunSectionKey, RunEntry[]> = {
    running: ingestion.running.map(taskEntry),
    actionable: ingestion.actionable.map(taskEntry),
    completed: ingestion.completed.map(taskEntry),
  };
  input.nodes
    .filter((node) => node.type === "experiment")
    .forEach((node) => {
      const run = buildExperimentRun(
        node,
        experimentControl[node.id] ?? null,
        input.tasks,
        watchers,
      );
      sections[
        experimentRunSection(
          run.health,
          run.currentTask?.status ??
            asAgentTaskStatus(run.control?.operational?.current_status ?? null),
        )
      ].push(experimentEntry(run));
    });
  input.nodes
    .filter((node) => node.type === "blocker" && node.status === "open")
    .forEach((node) => {
      sections.actionable.push({
        kind: "blocker",
        id: node.id,
        observedAt: newestTimestamp(node.source_refs.map((source) => source.timestamp)),
        node,
      });
    });
  return {
    running: sortRunEntries(sections.running),
    actionable: sortRunEntries(sections.actionable),
    completed: sortRunEntries(sections.completed),
  };
}

export function groupAgentTasks(tasks: AgentTask[]): AgentTaskGroup[] {
  const byId = new Map(tasks.map((task) => [task.operation_id, task]));
  const grouped = new Map<string, AgentTask[]>();
  tasks.forEach((task) => {
    const rootId = logicalRootId(task, byId);
    const attempts = grouped.get(rootId) ?? [];
    attempts.push(task);
    grouped.set(rootId, attempts);
  });
  return [...grouped.entries()]
    .map(([rootId, attempts]) => {
      attempts.sort(compareTaskAscending);
      return {
        rootId,
        root: byId.get(rootId) ?? attempts[0],
        latest: attempts.at(-1) ?? attempts[0],
        attempts,
      };
    })
    .sort((left, right) => compareTaskAscending(right.latest, left.latest));
}

function currentExperimentTaskGroup(
  nodeId: string,
  control: ExperimentControlState | null,
  tasks: AgentTask[],
): { taskGroup: AgentTaskGroup | null; currentTask: AgentTask | null } {
  const nodeTasks = tasks.filter(
    (task) => isExperimentLoopTask(task) && task.request.control_node_id === nodeId,
  );
  const currentOperationId = control?.operational?.current_operation_id ?? null;
  const currentTask = currentOperationId
    ? (nodeTasks.find((task) => task.operation_id === currentOperationId) ?? null)
    : null;
  const episodeTasks = control?.episode_id
    ? nodeTasks.filter((task) => taskEpisodeId(task) === control.episode_id)
    : nodeTasks;
  const groups = groupAgentTasks(episodeTasks);
  const taskGroup =
    (currentOperationId
      ? groups.find((group) =>
          group.attempts.some((task) => task.operation_id === currentOperationId),
        )
      : null) ??
    groups[0] ??
    null;
  return { taskGroup, currentTask: currentTask ?? taskGroup?.latest ?? null };
}

function taskEpisodeId(task: AgentTask): string | null {
  const value = task.request.control_episode_id;
  return typeof value === "string" && value ? value : null;
}

function asAgentTaskStatus(value: string | null): AgentTaskStatus | null {
  return value &&
    (actionableStatuses.has(value) || runningStatuses.has(value) || value === "succeeded")
    ? (value as AgentTaskStatus)
    : null;
}

function taskEntry(group: AgentTaskGroup): RunEntry {
  return { kind: "task", id: group.rootId, observedAt: group.latest.updated_at, group };
}

function experimentEntry(experiment: ExperimentRun): RunEntry {
  const observedAt = newestTimestamp([
    experiment.taskGroup?.latest.updated_at,
    experiment.control?.operational?.current_last_activity_at,
    ...experiment.watchers.flatMap((watcher) => [
      watcher.completed_at,
      watcher.last_checked_at,
      watcher.created_at,
    ]),
  ]);
  return { kind: "experiment", id: experiment.node.id, observedAt, experiment };
}

/** An Experiment-loop watcher is released by Stop loop, never one watcher at a time. */
export function watcherIsIndividuallyStoppable(watcher: WatcherRecord): boolean {
  return watcher.continuation?.patch_kind !== "experiment_loop";
}

function sortRunEntries(entries: RunEntry[]): RunEntry[] {
  return [...entries].sort((left, right) => {
    const leftAt = left.observedAt ? Date.parse(left.observedAt) : Number.NaN;
    const rightAt = right.observedAt ? Date.parse(right.observedAt) : Number.NaN;
    const leftKnown = Number.isFinite(leftAt);
    const rightKnown = Number.isFinite(rightAt);
    if (leftKnown && rightKnown && leftAt !== rightAt) return rightAt - leftAt;
    if (leftKnown !== rightKnown) return leftKnown ? -1 : 1;
    return left.id.localeCompare(right.id);
  });
}

function newestTimestamp(values: (string | null | undefined)[]): string | null {
  return (
    values
      .filter(
        (value): value is string => typeof value === "string" && Number.isFinite(Date.parse(value)),
      )
      .sort((left, right) => Date.parse(right) - Date.parse(left))[0] ?? null
  );
}

function logicalRootId(task: AgentTask, byId: Map<string, AgentTask>): string {
  const seen = new Set([task.operation_id]);
  let current = task;
  while (
    current.parent_operation_id &&
    byId.has(current.parent_operation_id) &&
    !seen.has(current.parent_operation_id)
  ) {
    seen.add(current.parent_operation_id);
    current = byId.get(current.parent_operation_id) as AgentTask;
  }
  return current.operation_id;
}

function compareTaskAscending(left: AgentTask, right: AgentTask): number {
  return (
    left.created_at.localeCompare(right.created_at) ||
    left.operation_id.localeCompare(right.operation_id)
  );
}

function isTaskNotificationSuperseded(task: AgentTask, tasks: AgentTask[]): boolean {
  if (
    (task.kind !== "seed" && task.kind !== "refresh") ||
    (task.status !== "failed" && task.status !== "interrupted")
  )
    return false;
  return tasks.some(
    (candidate) =>
      (candidate.kind === "seed" || candidate.kind === "refresh") &&
      candidate.status === "succeeded" &&
      compareTaskAscending(candidate, task) > 0,
  );
}
