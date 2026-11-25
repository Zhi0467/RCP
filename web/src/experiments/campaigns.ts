import type {
  AgentTask,
  Episode,
  EpisodeBlockedReason,
  EpisodeEnding,
  RunAction,
  RunPresentation,
  RunRecommendation,
} from "../core/types";

export type EpisodeTaskRole = "orchestrator" | "worker" | "wake";

export function isLiveEpisode(episode: Episode): boolean {
  // The projection says whether a parent is still live. Restating the storage
  // status list here is how two surfaces came to disagree about one Experiment.
  return episode.live;
}

export interface AutoResearchStartState {
  projectOpen: boolean;
  mutationsDisabled: boolean;
  authoritative: boolean;
  canonicalReachable: boolean;
  taskStarting: boolean;
  episodeAction: string | null;
}

/** Why Auto-research cannot be authorized now, or null. The header button and the
 * agent tool both refuse through this one check. */
export function autoResearchStartRefusal(state: AutoResearchStartState): string | null {
  if (!state.projectOpen) return "No RCP project is open.";
  if (state.mutationsDisabled) return "Graph mutations are currently disabled.";
  if (!state.authoritative) return "The project has not finished reconciling its saved state.";
  if (!state.canonicalReachable) return "The project's saved state is unreachable.";
  if (state.taskStarting) return "Another turn is already starting.";
  if (state.episodeAction) return "Wait for the current episode action to finish.";
  return null;
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
  openedEpisodeId: string | null = null,
): Episode[] {
  // A continuation chain is one run whose newest member is the card, so the
  // newest member decides the chain's visibility; archiving it hides the run.
  // An Auto-research child is listed on its orchestrator's card; it gets its
  // own card only while a route opens it from there.
  return [...episodes]
    .sort(compareEpisodesNewestFirst)
    .filter((episode) => !episode.continued_by_episode_id)
    .filter(
      (episode) =>
        !episode.auto_research_parent_episode_id || episode.episode_id === openedEpisodeId,
    )
    .filter(
      (episode) =>
        (!episode.archived || showArchived) &&
        (episode.mode === "auto_research" ||
          currentExperimentEpisodeIds.has(episode.episode_id) ||
          (episode.archived && showArchived)),
    );
}

const RUN_RECOMMENDATION_LABELS: Record<RunRecommendation, string> = {
  continue: "Let the run continue",
  wait: "Wait for the current step",
  resume: "Resume the current turn",
  retry: "Retry the current turn",
  reauthorize: "Add turns",
  reauthenticate_provider: "Sign in on the machine, verify it above, then retry",
  keep_loop: "Keep the loop running; check now if needed",
  start_episode: "Start a new episode",
  stop_and_restart: "Stop the loop, then start a new episode",
  resolve_requirements: "Resolve the run requirements",
  open_report: "Open report",
  review: "Review the run",
  none: "No action needed",
};

/** The published next step in words, led by what blocks the run when something does. */
export function runRecommendationLabel(
  presentation: Pick<RunPresentation, "recommendation" | "actions">,
  episode: Pick<Episode, "blocked_reason" | "ending"> | null,
): string {
  const offered = (kind: RunAction["kind"]) =>
    presentation.actions.some((action) => action.kind === kind);
  const step =
    presentation.recommendation === "reauthorize" && !offered("continue")
      ? "review"
      : presentation.recommendation;
  const label = RUN_RECOMMENDATION_LABELS[step];
  if (step === "reauthenticate_provider") return label;
  return blockedReasonLead(episode?.blocked_reason, episode?.ending, label);
}

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

/**
 * Whether a retry may move to another provider. A rebinding starts a clean
 * native session, which an Auto-research worker never gets and a stopping
 * episode refuses from anyone.
 */
export function episodeCanSwitchProvider(episode: Episode, task: AgentTask): boolean {
  return (
    episode.stop_requested_at === null &&
    episode.tasks.find((member) => member.operation_id === task.operation_id)?.role !== "worker"
  );
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
