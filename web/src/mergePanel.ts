import type { EpisodeUnfinishedJob, MergeDiffPath, MergeEpisodeBody, MergePreview } from "./types";

export interface MergeChoices {
  targetBranch: string;
  historyMode: "merge" | "squash";
  keepBranchOpen: boolean;
  removeWorktree: boolean;
  deleteCodeBranch: boolean;
}

export interface MergeDiffCounts {
  changed: number;
  delivered: number;
  proposals: number;
  conflicts: number;
  needsAgent: number;
}

export type MergeDiffMark = "delivered" | "changed" | "proposal" | "needs_agent" | "conflict";

export const MERGE_MARK_SEVERITY: MergeDiffMark[] = [
  "delivered",
  "changed",
  "proposal",
  "needs_agent",
  "conflict",
];

export function mergePathMark(path: MergeDiffPath): MergeDiffMark {
  if (path.conflict) return "conflict";
  if (path.needs_agent) return "needs_agent";
  if (path.needs_proposal) return "proposal";
  return path.delivered ? "delivered" : "changed";
}

/** Each entity's mark is its most severe path. */
export function mergeDiffMarks(paths: MergeDiffPath[]): Map<string, MergeDiffMark> {
  const marks = new Map<string, MergeDiffMark>();
  for (const path of paths) {
    const key = `${path.entity}:${path.id}`;
    const mark = mergePathMark(path);
    const current = marks.get(key);
    if (!current || MERGE_MARK_SEVERITY.indexOf(mark) > MERGE_MARK_SEVERITY.indexOf(current))
      marks.set(key, mark);
  }
  return marks;
}

export function mergeDiffCounts(paths: MergeDiffPath[]): MergeDiffCounts {
  const counts: MergeDiffCounts = {
    changed: 0,
    delivered: 0,
    proposals: 0,
    conflicts: 0,
    needsAgent: 0,
  };
  for (const mark of mergeDiffMarks(paths).values()) {
    if (mark === "delivered") counts.delivered += 1;
    else counts.changed += 1;
    if (mark === "proposal") counts.proposals += 1;
    if (mark === "conflict") counts.conflicts += 1;
    // Every residue path goes to the merge agent, conflicts and Proposals included.
    if (mark !== "changed" && mark !== "delivered") counts.needsAgent += 1;
  }
  return counts;
}

/** An agent lands code only with a merge commit; squash stays for agentless merges. */
export function squashAllowed(preview: MergePreview): boolean {
  return preview.code !== null && !preview.needs_agent;
}

export function mergeRequestBody(
  preview: MergePreview,
  choices: MergeChoices,
  hasGraphBranch: boolean,
): MergeEpisodeBody {
  const squash = choices.historyMode === "squash" && squashAllowed(preview);
  const keepBranchOpen = hasGraphBranch && !squash && choices.keepBranchOpen;
  const body: MergeEpisodeBody = {
    history_mode: squash ? "squash" : "merge",
    keep_branch_open: keepBranchOpen,
    archive_graph_branch: hasGraphBranch && !keepBranchOpen,
    remove_worktree: preview.code !== null && choices.removeWorktree,
    delete_code_branch: preview.code !== null && choices.removeWorktree && choices.deleteCodeBranch,
  };
  if (preview.code && choices.targetBranch.trim()) body.target_branch = choices.targetBranch.trim();
  return body;
}

/** Merge may use a preview only once it answered the target the human typed. */
export function previewAnswersDraft(
  preview: MergePreview | null,
  previewTarget: string | null | undefined,
  target: string | null,
  targetDraft: string,
): preview is MergePreview {
  const draft = targetDraft.trim();
  return (
    preview !== null &&
    previewTarget === target &&
    (!preview.code || !draft || preview.code.target_branch === draft)
  );
}

/** The jobs a Merge paused on, or null when the error is not that pause. */
export function unfinishedJobsFromError(error: unknown): EpisodeUnfinishedJob[] | null {
  const detail = (error as { detail?: { code?: unknown; jobs?: unknown } } | null)?.detail;
  return detail?.code === "unfinished_jobs_confirmation_required" && Array.isArray(detail.jobs)
    ? (detail.jobs as EpisodeUnfinishedJob[])
    : null;
}

export type BranchDiffWord = MergeDiffMark | "created" | "updated" | "removed";

/** The one word a changed node shows: how Merge treats it, else how the branch changed it. */
export function branchDiffWord(
  change: "created" | "updated" | "removed",
  mark: MergeDiffMark | undefined,
): BranchDiffWord {
  return mark && mark !== "changed" ? mark : change;
}
