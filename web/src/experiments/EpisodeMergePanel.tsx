import { ChevronDown, LoaderCircle, Network, RotateCcw } from "lucide-react";
import { useEffect, useState } from "react";
import { loadMergePreview } from "../core/api";
import {
  type MergeChoices,
  mergeRequestBody,
  previewAnswersDraft,
  squashAllowed,
  unfinishedJobsFromError,
} from "../graph/mergePanel";
import type { Episode, EpisodeUnfinishedJob, MergeEpisodeBody, MergePreview } from "../core/types";

const CODE_STATUS_LABELS: Record<NonNullable<MergePreview["code"]>["status"], string> = {
  clean: "Merges cleanly",
  conflict: "Code conflicts",
  already_merged: "Already merged",
  target_dirty: "Target checkout has uncommitted changes",
  target_missing: "Target branch not found",
};

export function EpisodeMergePanel({
  apiBase,
  episode,
  disabled,
  busy,
  onMerge,
}: {
  apiBase: string;
  episode: Episode;
  disabled: boolean;
  busy: boolean;
  onMerge: (episodeId: string, body: MergeEpisodeBody) => Promise<void>;
}) {
  const [preview, setPreview] = useState<MergePreview | null>(null);
  const [previewKey, setPreviewKey] = useState<string | null>(null);
  // The target the shown preview answered; Merge waits until it matches the typed one.
  const [previewTarget, setPreviewTarget] = useState<string | null | undefined>(undefined);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [mergeError, setMergeError] = useState<string | null>(null);
  // Merge paused on these jobs; the human can merge anyway and the agent stops them.
  const [pausedJobs, setPausedJobs] = useState<EpisodeUnfinishedJob[] | null>(null);
  const [open, setOpen] = useState(false);
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [targetDraft, setTargetDraft] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [choices, setChoices] = useState<MergeChoices>({
    targetBranch: "",
    historyMode: "merge",
    keepBranchOpen: false,
    removeWorktree: true,
    deleteCodeBranch: true,
  });
  // A new branch head, attempt, target, or episode update (a turn's new code) means a new preview.
  const refreshKey = JSON.stringify([
    episode.status,
    episode.updated_at,
    episode.graph_branch,
    episode.isolation_state?.merge_attempt?.attempt_id,
    episode.isolation_state?.status,
    target,
    previewAttempt,
  ]);

  // A refusal belongs to the branch state it answered; a new snapshot retires it.
  const branchSnapshot = JSON.stringify(episode.graph_branch);
  useEffect(() => {
    setMergeError(null);
    setPreviewError(null);
  }, [branchSnapshot]);

  useEffect(() => {
    if (!episode.can_merge) setOpen(false);
  }, [episode.can_merge]);

  useEffect(() => {
    if (!open || !episode.can_merge) return;
    let current = true;
    setPreviewError(null);
    // The old preview no longer answers this key; Merge waits for the new one.
    setPreviewTarget(undefined);
    loadMergePreview(apiBase, episode.episode_id, target)
      .then((next) => {
        if (!current) return;
        setPreview(next);
        setPreviewKey(refreshKey);
        setPreviewTarget(target);
        if (next.code && !target) setTargetDraft(next.code.target_branch);
      })
      .catch((error) => {
        if (current) {
          setPreview(null);
          setPreviewError(error instanceof Error ? error.message : String(error));
        }
      });
    return () => {
      current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- refreshKey already carries target and the episode fields that need a new preview
  }, [apiBase, episode.episode_id, episode.can_merge, refreshKey, open]);

  const hasGraphBranch = episode.graph_branch !== null;
  const squash = preview !== null && squashAllowed(preview);
  const historyMode = squash ? choices.historyMode : "merge";
  const previewCurrent =
    previewKey === refreshKey && previewAnswersDraft(preview, previewTarget, target, targetDraft);
  const previewLoading = open && previewKey !== refreshKey && !previewError;

  const merge = async (confirmUnfinishedJobs = false) => {
    if (!preview || !previewCurrent || disabled || !episode.can_merge) return;
    setMergeError(null);
    setPausedJobs(null);
    const body = mergeRequestBody(
      preview,
      { ...choices, targetBranch: targetDraft },
      hasGraphBranch,
    );
    try {
      await onMerge(
        episode.episode_id,
        confirmUnfinishedJobs ? { ...body, confirm_unfinished_jobs: true } : body,
      );
      setOpen(false);
    } catch (error) {
      const jobs = unfinishedJobsFromError(error);
      if (jobs) setPausedJobs(jobs);
      else setMergeError(error instanceof Error ? error.message : String(error));
    }
  };

  const diagnostic = mergeError ?? previewError ?? episode.graph_branch?.merge_diagnostic;
  if (!episode.can_merge && !diagnostic) return null;

  return (
    <div className={`episode-merge-panel${diagnostic ? " has-error" : ""}`} aria-label="Merge">
      <div className="merge-summary-row">
        <div className="merge-counts">
          {hasGraphBranch && preview && <MergeGraphCounts graph={preview.graph} />}
          {preview?.code && (
            <span>
              {preview.code.commits_ahead} {preview.code.commits_ahead === 1 ? "commit" : "commits"}{" "}
              in {preview.code.repo_alias}
            </span>
          )}
        </div>
        <button
          className={`button secondary compact campaign-branch-merge${diagnostic ? " icon-button" : ""}`}
          type="button"
          aria-expanded={open}
          aria-busy={busy || previewLoading}
          aria-label={diagnostic ? "Retry merge" : undefined}
          title={
            diagnostic
              ? [diagnostic, !episode.can_merge && episode.graph_branch?.merge_blocked_reason]
                  .filter(Boolean)
                  .join("\n")
              : undefined
          }
          disabled={disabled || !episode.can_merge}
          onClick={() => {
            if (diagnostic) {
              setMergeError(null);
              setOpen(true);
              setPreviewAttempt((value) => value + 1);
            } else setOpen(!open);
          }}
        >
          {busy || previewLoading ? (
            <LoaderCircle className="spin" size={12} />
          ) : diagnostic ? (
            <RotateCcw size={12} />
          ) : (
            <Network size={12} />
          )}
          {!diagnostic &&
            (busy ? "Starting merge…" : previewLoading ? "Loading preview…" : "Merge")}
          {!busy && !diagnostic && <ChevronDown size={12} />}
        </button>
      </div>
      {episode.isolation_state?.merge_attempt?.worktree_kept && (
        <span className="merge-note">
          Merged, but the worktree was kept: a job was still running. Merge again once it ends to
          remove it.
        </span>
      )}
      {episode.can_merge && open && preview && (
        <fieldset className="merge-pickers" aria-label="Merge options" disabled={disabled}>
          {preview.code && (
            <>
              <label className="merge-picker">
                <span>Into</span>
                <input
                  value={targetDraft}
                  onChange={(event) => setTargetDraft(event.target.value)}
                  onBlur={() => setTarget(targetDraft.trim() || null)}
                />
              </label>
              <label className="merge-picker">
                <span>Code history</span>
                <select
                  value={historyMode}
                  onChange={(event) =>
                    setChoices({
                      ...choices,
                      historyMode: event.target.value === "squash" ? "squash" : "merge",
                    })
                  }
                >
                  <option value="merge">Merge commit</option>
                  <option value="squash" disabled={!squash}>
                    Squash
                  </option>
                </select>
              </label>
              {preview.code.status !== "clean" &&
                // Leftovers are committed at Merge, so a tip equal to the target is not merged yet.
                !(
                  preview.code.status === "already_merged" && preview.code.leftover_files.length
                ) && (
                  <span className={`merge-code-status merge-code-${preview.code.status}`}>
                    {CODE_STATUS_LABELS[preview.code.status]}
                  </span>
                )}
              {preview.code.conflict_files.length > 0 && (
                <ul className="merge-conflict-files" aria-label="Conflicting files">
                  {preview.code.conflict_files.map((path) => (
                    <li key={path}>{path}</li>
                  ))}
                </ul>
              )}
              {preview.code.leftover_files.length > 0 && (
                <span className="merge-note">
                  {preview.code.leftover_files.length} uncommitted{" "}
                  {preview.code.leftover_files.length === 1 ? "file" : "files"} will be committed.
                </span>
              )}
              <label className="merge-check">
                <input
                  type="checkbox"
                  checked={choices.removeWorktree}
                  onChange={(event) =>
                    setChoices({
                      ...choices,
                      removeWorktree: event.target.checked,
                      deleteCodeBranch: event.target.checked,
                    })
                  }
                />
                <span>
                  Remove the Git worktree and its branch <code>{preview.code.source_branch}</code>
                </span>
              </label>
            </>
          )}
          {hasGraphBranch && (
            <label className="merge-check">
              <input
                type="checkbox"
                checked={historyMode !== "squash" && choices.keepBranchOpen}
                disabled={historyMode === "squash"}
                onChange={(event) =>
                  setChoices({ ...choices, keepBranchOpen: event.target.checked })
                }
              />
              <span>Keep the graph branch for more runs</span>
            </label>
          )}
          <div className="merge-picker-actions">
            <button
              className="button secondary compact"
              type="button"
              onClick={() => setOpen(false)}
            >
              Cancel
            </button>
            <button
              className="button primary compact"
              type="button"
              disabled={!previewCurrent}
              onClick={() => void merge()}
            >
              Merge
            </button>
          </div>
        </fieldset>
      )}
      {pausedJobs && (
        <div className="merge-unfinished-jobs" role="alert">
          <strong>These jobs may still be writing the worktree.</strong>
          <ul aria-label="Unfinished jobs">
            {pausedJobs.map((job) => (
              <li key={`${job.kind}:${job.id}`}>
                <code>{job.command ?? job.id}</code> <span>{job.status}</span>
              </li>
            ))}
          </ul>
          <span>Merge anyway, and the merge agent stops them before it merges.</span>
          <div className="merge-unfinished-actions">
            <button
              className="button primary compact"
              type="button"
              disabled={disabled || !previewCurrent}
              onClick={() => void merge(true)}
            >
              Merge anyway
            </button>
            <button
              className="button secondary compact"
              type="button"
              onClick={() => setPausedJobs(null)}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

export function MergeGraphCounts({ graph }: { graph: MergePreview["graph"] }) {
  return (
    <>
      <span className="branch-diff-word diff-changed">{graph.direct} direct</span>
      <span className="branch-diff-word diff-conflict" title="Agent answers">
        {graph.conflicts} conflicts
      </span>
      <span className="branch-diff-word diff-proposal" title="Pending Proposals">
        {graph.reviews} reviews
      </span>
    </>
  );
}
