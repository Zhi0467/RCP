import type {
  AgentTask,
  Episode,
  EpisodeBlockedReason,
  EpisodeEnding,
  EpisodeHealth,
  EpisodeRecommendationKind,
  EpisodeTaskControlKind,
  EpisodeTask,
} from "./types";

export type { EpisodeHealth, EpisodeRecommendationKind };

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
  task: EpisodeTask;
  role: EpisodeTaskRole;
  depth: number;
}

export interface EpisodeRecommendation {
  kind: EpisodeRecommendationKind;
  label: string;
  task: AgentTask | null;
}

export interface EpisodeTaskControl {
  kind: EpisodeTaskControlKind;
  task: AgentTask;
  /**
   * Whether this control may be retried on a different binding. A rebinding
   * starts a clean native session, which an Auto-research worker never gets —
   * it continues only through the session its dispatch bound it to — and which
   * a stopping episode refuses from anyone, so it admits only exact recovery.
   * A switch either would submit comes back refused.
   */
  canSwitchProvider: boolean;
}

export interface EpisodeProjection {
  health: EpisodeHealth;
  healthLabel: string;
  recommendation: EpisodeRecommendation;
  taskControl: EpisodeTaskControl | null;
}

export function isLiveEpisode(episode: Episode): boolean {
  // The projection says whether a parent is still live. Restating the storage
  // status list here is how two surfaces came to disagree about one Experiment.
  return episode.live;
}

export function mergeEpisode(episodes: Episode[], nextEpisode: Episode): Episode[] {
  return [
    nextEpisode,
    ...episodes.filter((episode) => episode.episode_id !== nextEpisode.episode_id),
  ].sort(compareEpisodesNewestFirst);
}

export function runsEpisodeCards(
  episodes: Episode[],
  currentExperimentEpisodeIds: ReadonlySet<string>,
  showArchived = false,
): Episode[] {
  // A continuation chain is one run whose newest member is the card, so the
  // newest member decides the chain's visibility; archiving it hides the run.
  return [...episodes]
    .sort(compareEpisodesNewestFirst)
    .filter((episode) => !episode.continued_by_episode_id)
    .filter(
      (episode) =>
        (!episode.archived || showArchived) &&
        (episode.mode === "auto_research" ||
          currentExperimentEpisodeIds.has(episode.episode_id) ||
          (episode.archived && showArchived)),
    );
}

export function currentEpisodeControlTask(
  episode: Episode,
  tasks: AgentTask[] = episode.tasks,
): AgentTask | null {
  if (!episode.current_control_task_id) return null;
  return tasks.find((task) => task.operation_id === episode.current_control_task_id) ?? null;
}

const EPISODE_RECOMMENDATION_LABELS: Record<EpisodeRecommendationKind, string> = {
  continue: "Let auto-research continue",
  wait: "Wait for the current step",
  resume: "Resume the current turn",
  retry: "Retry the current turn",
  reauthorize: "Add turns",
  open_report: "Open report",
  review: "Review the episode state",
  none: "No further action is needed",
};

/** The wording for the wait and review the projection distinguishes by state. */
const EPISODE_RECOMMENDATION_LABELS_BY_HEALTH: Partial<
  Record<EpisodeHealth, Partial<Record<EpisodeRecommendationKind, string>>>
> = {
  wrapping_up: { wait: "Wrapping up visualization and report" },
  recovering: { wait: "Wait for automatic turn recovery" },
  stopping: { wait: "Wait for the current turn to finish" },
  starting: { wait: "Wait for auto-research to start" },
  active: { wait: "Wait for the current turn to pause" },
  stopped: { none: "The episode has stopped" },
  failed: { review: "Review the episode failure" },
  needs_action: { review: "Review the blocked turn" },
};

/**
 * One lead sentence naming what blocks the episode, before the recommendation.
 * The backend decides the block; the ending only chooses the wording for it.
 */
export function blockedReasonLead(
  reason: EpisodeBlockedReason | null | undefined,
  ending: string | null | undefined,
  label: string,
): string {
  if (reason === "sign_in") {
    return `The provider login is dead. Sign in again, then ${label.charAt(0).toLowerCase()}${label.slice(1)}`;
  }
  if (reason === "repeated_failure") {
    return `The retry failed the same way, so RCP stopped. Change the provider, model, or reasoning and ${label.charAt(0).toLowerCase()}${label.slice(1)}`;
  }
  if (reason === "reauthorize") {
    const lead =
      ending === "human_pause"
        ? "The episode paused for human authority."
        : ending === "exhausted"
          ? "The authorized turns are spent."
          : "More authorized turns are needed.";
    return `${lead} ${label}`;
  }
  return label;
}

export function awaitingDecisionLead(ids: string[] | undefined, label: string): string {
  const owed = ids?.length ?? 0;
  if (owed === 0) return label;
  const subject = owed === 1 ? "A Decision is" : `${owed} Decisions are`;
  return `${subject} waiting on your choice. ${label}`;
}

export function episodeProjection(
  episode: Episode,
  tasks: AgentTask[] = episode.tasks,
): EpisodeProjection {
  // Lifecycle state, next step, and available control are decided by the server.
  // This function resolves them to copy and to the task object a control acts on.
  const task = currentEpisodeControlTask(episode, tasks);
  const defaultLabel =
    EPISODE_RECOMMENDATION_LABELS_BY_HEALTH[episode.health]?.[episode.recommendation] ??
    EPISODE_RECOMMENDATION_LABELS[episode.recommendation];
  const label =
    episode.mode === "experiment_loop"
      ? experimentEpisodeRecommendationLabel(episode, defaultLabel)
      : defaultLabel;
  return {
    health: episode.health,
    healthLabel: EPISODE_HEALTH_LABELS[episode.health],
    recommendation: {
      kind: episode.recommendation,
      // A dead login or a spent authorization outranks the choice: neither the
      // episode nor the human can act on the Decision until it is cleared.
      label: episode.blocked_reason
        ? blockedReasonLead(episode.blocked_reason, episode.ending, label)
        : awaitingDecisionLead(episode.awaiting_decision_ids, label),
      task,
    },
    taskControl:
      episode.task_control && task
        ? {
            kind: episode.task_control,
            task,
            // Read from the episode's own membership: a mode without roles, or
            // a control this list does not name, is not a worker. A turn still
            // waiting to be collected is the third refusal: its control adopts
            // the finished turn on the host that ran it, which no new binding
            // can reach.
            canSwitchProvider:
              episode.stop_requested_at === null &&
              !task.can_collect &&
              episode.tasks.find((member) => member.operation_id === task.operation_id)?.role !==
                "worker",
          }
        : null,
  };
}

function experimentEpisodeRecommendationLabel(episode: Episode, fallback: string): string {
  if (episode.recommendation === "continue") return "Let the experiment loop continue";
  if (episode.health === "starting" && episode.recommendation === "wait") {
    return "Wait for the experiment loop to start";
  }
  if (episode.health === "active" && episode.recommendation === "wait") {
    return "Wait for the current experiment turn to pause";
  }
  return fallback;
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
  return `/api/projects/${encodeURIComponent(projectId)}/episodes/${encodeURIComponent(episodeId)}/report/viewer`;
}

export function episodeTaskRows(
  episode: Episode,
  tasks: EpisodeTask[] = episode.tasks,
): EpisodeTaskRow[] {
  const episodeTasks = tasks
    .filter((task) => task.episode_id === episode.episode_id)
    .sort(compareTaskTime);
  return episodeTasks.map((task) => ({
    task,
    role: task.role,
    depth: task.depth,
  }));
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

function compareTaskTime(left: EpisodeTask, right: EpisodeTask): number {
  return (
    comparableTime(left.created_at) - comparableTime(right.created_at) ||
    left.operation_id.localeCompare(right.operation_id)
  );
}

function comparableTime(value: string): number {
  const parsed = Date.parse(value);
  return Number.isNaN(parsed) ? 0 : parsed;
}

function compareEpisodesNewestFirst(left: Episode, right: Episode): number {
  return (
    comparableTime(right.created_at) - comparableTime(left.created_at) ||
    right.episode_id.localeCompare(left.episode_id)
  );
}
