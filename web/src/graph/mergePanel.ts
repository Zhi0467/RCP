import type {
  EpisodeUnfinishedJob,
  GraphRef,
  MergeDiffPath,
  MergeEpisodeBody,
  MergePreview,
} from "../core/types";

/** App-owned admission, shared by run cards and branch pickers. */
export type BranchMergeRequest = (
  source: string | Extract<GraphRef, { kind: "branch" }>,
  body?: MergeEpisodeBody,
  requestId?: string,
  projectId?: string,
) => Promise<void>;

/** Picker merges retain the branch and its code workspace for further work. */
export function pickerMergeRequestBody(confirmUnfinishedJobs: boolean): MergeEpisodeBody {
  return {
    history_mode: "merge",
    keep_branch_open: true,
    archive_graph_branch: false,
    remove_worktree: false,
    delete_code_branch: false,
    ...(confirmUnfinishedJobs ? { confirm_unfinished_jobs: true } : {}),
  };
}

export interface MergeChoices {
  targetBranch: string;
  historyMode: "merge" | "squash";
  keepBranchOpen: boolean;
  removeWorktree: boolean;
  deleteCodeBranch: boolean;
}

export type MergeDiffMark = "delivered" | "changed" | "proposal" | "conflict";

export const MERGE_MARK_SEVERITY: MergeDiffMark[] = [
  "delivered",
  "changed",
  "proposal",
  "conflict",
];

export function mergePathMark(path: MergeDiffPath): MergeDiffMark {
  if (path.conflict) return "conflict";
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
