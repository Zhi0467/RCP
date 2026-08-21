import type { AgentTask, Episode, EpisodeEnding, EpisodeStatus } from "./types";

const LIVE_EPISODE_STATUSES = new Set<EpisodeStatus>([
  "queued",
  "running",
  "stopping",
  "wrapping_up",
]);

const EPISODE_HEALTH_LABELS: Record<EpisodeHealth, string> = {
  starting: "Starting",
  active: "Active",
  recovering: "Recovering",
  needs_action: "Needs action",
  stopping: "Stopping gracefully",
  wrapping_up: "Wrapping up visualization and report",
  completed: "Completed",
  stopped: "Stopped",
  failed: "Failed",
};

export type EpisodeTaskRole = "orchestrator" | "worker" | "wake";

export interface EpisodeTaskRow {
  task: AgentTask;
  role: EpisodeTaskRole;
  depth: number;
}

export type EpisodeHealth =
  | "starting"
  | "active"
  | "recovering"
  | "needs_action"
  | "stopping"
  | "wrapping_up"
  | "completed"
  | "stopped"
  | "failed";

export type EpisodeRecommendationKind =
  "continue" | "wait" | "resume" | "retry" | "reauthorize" | "open_report" | "review" | "none";

export interface EpisodeRecommendation {
  kind: EpisodeRecommendationKind;
  label: string;
  task: AgentTask | null;
}

export interface EpisodeTaskControl {
  kind: "pause" | "resume" | "retry";
  task: AgentTask;
}

type EpisodeRecoveryControl = EpisodeTaskControl & { kind: "resume" | "retry" };

export interface EpisodeProjection {
  health: EpisodeHealth;
  healthLabel: string;
  recommendation: EpisodeRecommendation;
  taskControl: EpisodeTaskControl | null;
}

export function isLiveEpisode(episode: Episode): boolean {
  return LIVE_EPISODE_STATUSES.has(episode.status);
}

export function mergeEpisode(episodes: Episode[], nextEpisode: Episode): Episode[] {
  return [
    nextEpisode,
    ...episodes.filter((episode) => episode.episode_id !== nextEpisode.episode_id),
  ].sort(
    (left, right) =>
      comparableTime(right.created_at) - comparableTime(left.created_at) ||
      right.episode_id.localeCompare(left.episode_id),
  );
}

export function currentEpisodeControlTask(
  episode: Episode,
  tasks: AgentTask[] = episode.tasks,
): AgentTask | null {
  if (!episode.current_control_task_id) return null;
  return tasks.find((task) => task.operation_id === episode.current_control_task_id) ?? null;
}

export function episodeProjection(
  episode: Episode,
  tasks: AgentTask[] = episode.tasks,
): EpisodeProjection {
  const task = currentEpisodeControlTask(episode, tasks);

  if (episode.wrapup_state === "pending" || episode.wrapup_state === "running") {
    return projectEpisode(
      "wrapping_up",
      recommendation("wait", "Wrapping up visualization and report", task),
    );
  }

  if (episode.report && episode.wrapup_state === "ready") {
    return projectEpisode(
      episode.status === "completed"
        ? "completed"
        : episode.status === "failed"
          ? "failed"
          : "needs_action",
      recommendation("open_report", "Open report", task),
    );
  }

  if (episode.status === "stopped") {
    return projectEpisode(
      "stopped",
      recommendation("none", "No further action is available", task),
    );
  }

  if (episode.status === "completed" || episode.status === "failed") {
    return projectEpisode(
      episode.status,
      recommendation(
        episode.status === "failed" ? "review" : "none",
        episode.status === "failed" ? "Review the episode failure" : "No further action is needed",
        task,
      ),
    );
  }

  if (episode.recovery?.status === "pending") {
    return projectEpisode(
      "recovering",
      recommendation("wait", "Wait for automatic turn recovery", task),
    );
  }

  if (episode.status === "needs_action" && episode.can_reauthorize) {
    return projectEpisode(
      "needs_action",
      recommendation("reauthorize", "Start a new authorized episode", task),
    );
  }

  const recoveryControl = episodeRecoveryControl(task);
  if (recoveryControl) {
    return projectEpisode(
      "needs_action",
      recommendation(
        recoveryControl.kind,
        recoveryControl.kind === "resume" ? "Resume the current turn" : "Retry the current turn",
        recoveryControl.task,
      ),
      recoveryControl,
    );
  }

  if (episode.status === "stopping") {
    return projectEpisode(
      "stopping",
      recommendation("wait", "Wait for the current turn to finish", task),
    );
  }

  if (
    task &&
    (task.status === "paused" || task.status === "interrupted" || task.status === "failed")
  ) {
    return projectEpisode(
      "needs_action",
      recommendation("review", "Review the blocked turn", task),
    );
  }

  if (episode.status === "queued" || task?.status === "queued") {
    return projectEpisode(
      "starting",
      recommendation("wait", "Wait for auto-research to start", task),
    );
  }

  if (episode.status === "needs_action") {
    return projectEpisode(
      "needs_action",
      recommendation("review", "Review the episode state", task),
    );
  }

  if (task?.status === "pausing") {
    return projectEpisode(
      "active",
      recommendation("wait", "Wait for the current turn to pause", task),
    );
  }

  return projectEpisode(
    "active",
    recommendation("continue", "Let auto-research continue", task),
    pauseControl(task),
  );
}

function projectEpisode(
  health: EpisodeHealth,
  next: EpisodeRecommendation,
  taskControl: EpisodeTaskControl | null = null,
): EpisodeProjection {
  return {
    health,
    healthLabel: EPISODE_HEALTH_LABELS[health],
    recommendation: next,
    taskControl,
  };
}

function recommendation(
  kind: EpisodeRecommendationKind,
  label: string,
  task: AgentTask | null,
): EpisodeRecommendation {
  return { kind, label, task };
}

function episodeRecoveryControl(task: AgentTask | null): EpisodeRecoveryControl | null {
  if (!task) return null;
  if (task.status === "paused") {
    if (task.can_resume) return { kind: "resume", task };
    if (task.can_retry) return { kind: "retry", task };
  }
  if (task.status === "interrupted" || task.status === "failed") {
    if (task.can_retry) return { kind: "retry", task };
    if (task.can_resume) return { kind: "resume", task };
  }
  return null;
}

function pauseControl(task: AgentTask | null): EpisodeTaskControl | null {
  return task?.status === "running" && task.can_pause ? { kind: "pause", task } : null;
}

export function episodeEndingLabel(ending: EpisodeEnding): string {
  switch (ending) {
    case "completed":
      return "Completed";
    case "exhausted":
      return "Exhausted";
    case "stopped":
      return "Stopped";
    case "failed":
      return "Failed";
    case "human_pause":
      return "Human-authority pause";
  }
}

export function episodeReportPreviewUrl(projectId: string, episodeId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/episodes/${encodeURIComponent(episodeId)}/report/preview`;
}

export function episodeTaskRows(episode: Episode, tasks: AgentTask[]): EpisodeTaskRow[] {
  const episodeTasks = tasks
    .filter((task) => task.episode_id === episode.episode_id)
    .sort(compareTaskTime);
  const byId = new Map(episodeTasks.map((task) => [task.operation_id, task]));
  return episodeTasks.map((task) => ({
    task,
    role: episodeTaskRole(episode, task),
    depth: episodeTaskDepth(task, byId),
  }));
}

export function episodeTaskRole(episode: Episode, task: AgentTask): EpisodeTaskRole {
  const declared = task.request.role ?? task.request.invocation_role;
  if (
    task.request.wake_cause ||
    task.request.trigger === "watcher" ||
    task.request.continuation_cause === "graph_condition" ||
    task.request.continuation_cause === "message"
  ) {
    return "wake";
  }
  if (declared === "worker") return "worker";
  if (task.operation_id === episode.root_operation_id) return "orchestrator";
  if (declared === "orchestrator" || declared === "wake") return declared;
  if (task.kind !== "auto_research" || task.request.control_node_id || task.request.node_id) {
    return "worker";
  }
  return "orchestrator";
}

export function episodeTaskRoleLabel(role: EpisodeTaskRole): string {
  switch (role) {
    case "orchestrator":
      return "Orchestrator";
    case "worker":
      return "Worker";
    case "wake":
      return "Wake";
  }
}

export function formatTokenCount(value: number): string {
  return new Intl.NumberFormat("en-US").format(value);
}

function episodeTaskDepth(task: AgentTask, byId: ReadonlyMap<string, AgentTask>): number {
  const actorOperationId = episodeActorOperationId(task);
  const actorOrigin = actorOperationId === null ? null : byId.get(actorOperationId);
  if (actorOrigin) return episodeActorDepth(actorOrigin, byId);
  return episodeAncestryDepth(task, byId);
}

function episodeActorDepth(task: AgentTask, byId: ReadonlyMap<string, AgentTask>): number {
  let depth = 0;
  let current = task;
  let parentId = current.parent_operation_id;
  const seen = new Set<string>([current.operation_id]);
  while (parentId && byId.has(parentId) && !seen.has(parentId)) {
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (!parent) break;
    if (
      (episodeActorOperationId(current) ?? current.operation_id) !==
      (episodeActorOperationId(parent) ?? parent.operation_id)
    ) {
      depth += 1;
    }
    current = parent;
    parentId = current.parent_operation_id;
  }
  return Math.min(depth, 4);
}

function episodeActorOperationId(task: AgentTask): string | null {
  const value = task.request.actor_operation_id;
  return typeof value === "string" && value.length > 0 ? value : null;
}

function episodeAncestryDepth(task: AgentTask, byId: ReadonlyMap<string, AgentTask>): number {
  let depth = 0;
  let parentId = task.parent_operation_id;
  const seen = new Set<string>();
  while (parentId && byId.has(parentId) && !seen.has(parentId)) {
    seen.add(parentId);
    depth += 1;
    parentId = byId.get(parentId)?.parent_operation_id ?? null;
  }
  return Math.min(depth, 4);
}

function compareTaskTime(left: AgentTask, right: AgentTask): number {
  return (
    comparableTime(left.created_at) - comparableTime(right.created_at) ||
    left.operation_id.localeCompare(right.operation_id)
  );
}

function comparableTime(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}
