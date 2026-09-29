import type { AppView, GraphTargetRef } from "./types";

export const MAIN_GRAPH: GraphTargetRef = { kind: "main" };

/** Browser state identity; project ids sent to the backend stay unchanged. */
export function graphSessionKey(projectId: string, target: GraphTargetRef = MAIN_GRAPH): string {
  return target.kind === "main" ? projectId : `${projectId}:branch:${target.branch_id}`;
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

export function graphViewHash(
  projectId: string,
  target: GraphTargetRef,
  view: AppView = "dag",
): string {
  const params = new URLSearchParams({ view: view === "execution" ? "runs" : view });
  if (target.kind === "branch") params.set("branch_id", target.branch_id);
  return `#/projects/${encodeURIComponent(projectId)}?${params}`;
}
