import type { AppView, Episode, ExperimentLoopIndexEntry, GraphTargetRef } from "./types";

export const MAIN_GRAPH: GraphTargetRef = { kind: "main" };

/** Browser state identity; project ids sent to the backend stay unchanged. */
export function graphSessionKey(projectId: string, target: GraphTargetRef = MAIN_GRAPH): string {
  return target.kind === "main" ? projectId : `${projectId}:branch:${target.branch_id}`;
}

/** The episode that owns a branch; a graph-archived Experiment stays in the index only. */
export function branchOwnerEpisode(
  target: GraphTargetRef,
  episodes: Episode[],
  experimentLoops: ExperimentLoopIndexEntry[],
): Episode | null {
  if (target.kind !== "branch") return null;
  return (
    [...episodes, ...experimentLoops.map((entry) => entry.episode)].find(
      (episode) =>
        episode.graph_branch?.branch_id === target.branch_id &&
        episode.episode_id === episode.graph_branch.current_episode_id,
    ) ?? null
  );
}

/** An isolated Experiment started from main runs on the branch named for its episode. */
export function experimentStartTarget(
  current: GraphTargetRef,
  graphIsolation: boolean | undefined,
  episodeId: string | null | undefined,
): GraphTargetRef {
  return graphIsolation && current.kind === "main" && episodeId
    ? { kind: "branch", branch_id: episodeId }
    : current;
}

export function sameGraphTarget(
  left: GraphTargetRef = MAIN_GRAPH,
  right: GraphTargetRef = MAIN_GRAPH,
): boolean {
  return left.kind === right.kind && (left.kind === "main" || left.branch_id === right.branch_id);
}

export function graphTargetUrl(path: string, target: GraphTargetRef = MAIN_GRAPH): string {
  if (target.kind === "main") return path;
  return `${path}${path.includes("?") ? "&" : "?"}branch_id=${encodeURIComponent(target.branch_id)}`;
}

export function graphTargetFromHash(hash: string): GraphTargetRef {
  const params = new URLSearchParams(hash.split("?")[1] ?? "");
  // Preserve an invalid supplied target for backend rejection; never fall back to main.
  return params.has("branch_id")
    ? { kind: "branch", branch_id: params.get("branch_id") ?? "" }
    : MAIN_GRAPH;
}

export interface ProjectHashSelection {
  chatId?: string;
  /** Exact Auto-research selection on Runs; Experiment routes carry additional identity. */
  autoResearchEpisodeId?: string;
}

/** null view preserves the existing bare project route. */
export function projectViewHash(
  projectId: string,
  target: GraphTargetRef,
  view: AppView | null,
  selection: ProjectHashSelection = {},
): string {
  const params = new URLSearchParams();
  if (view !== null) params.set("view", view === "execution" ? "runs" : view);
  if (selection.chatId !== undefined) params.set("chat", selection.chatId);
  if (selection.autoResearchEpisodeId !== undefined) {
    params.set("mode", "auto_research");
    params.set("episode", selection.autoResearchEpisodeId);
  }
  if (target.kind === "branch") params.set("branch_id", target.branch_id);
  return `#/projects/${encodeURIComponent(projectId)}${params.size ? `?${params}` : ""}`;
}

export function graphViewHash(
  projectId: string,
  target: GraphTargetRef,
  view: AppView = "dag",
): string {
  return projectViewHash(projectId, target, view);
}
