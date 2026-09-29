import { LoaderCircle, Network } from "lucide-react";
import { useEffect, useState } from "react";
import { loadMergePreview } from "../api";
import {
  type MergeChoices,
  mergeDiffCounts,
  mergeRequestBody,
  previewAnswersDraft,
  squashAllowed,
  unfinishedJobsFromError,
} from "../mergePanel";
import type { Episode, EpisodeUnfinishedJob, MergeEpisodeBody, MergePreview } from "../types";

const CODE_STATUS_LABELS: Record<NonNullable<MergePreview["code"]>["status"], string> = {
  clean: "Merges cleanly",
  conflict: "Conflicts",
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
  // The target the shown preview answered; Merge waits until it matches the typed one.
  const [previewTarget, setPreviewTarget] = useState<string | null | undefined>(undefined);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [mergeError, setMergeError] = useState<string | null>(null);
  // Merge paused on these jobs; the human can merge anyway and the agent stops them.
  const [pausedJobs, setPausedJobs] = useState<EpisodeUnfinishedJob[] | null>(null);
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
  ]);

  // A refusal belongs to the branch state it answered; a new snapshot retires it.
  const branchSnapshot = JSON.stringify(episode.graph_branch);
  useEffect(() => {
    setMergeError(null);
  }, [branchSnapshot]);

  useEffect(() => {
    let current = true;
    setPreviewError(null);
    // The old preview no longer answers this key; Merge waits for the new one.
    setPreviewTarget(undefined);
    loadMergePreview(apiBase, episode.episode_id, target)
      .then((next) => {
        if (!current) return;
        setPreview(next);
        setPreviewTarget(target);
        if (next.code && !target) setTargetDraft(next.code.target_branch);
      })
      .catch((error) => {
        if (current) setPreviewError(error instanceof Error ? error.message : String(error));
      });
    return () => {
      current = false;
    };
  }, [apiBase, episode.episode_id, refreshKey]);

  const hasGraphBranch = episode.graph_branch !== null;
  const counts = preview ? mergeDiffCounts(preview.graph.paths) : null;
  const squash = preview !== null && squashAllowed(preview);
  const historyMode = squash ? choices.historyMode : "merge";
  const previewCurrent = previewAnswersDraft(preview, previewTarget, target, targetDraft);

  const merge = async (confirmUnfinishedJobs = false) => {
    if (!preview || !previewCurrent || disabled) return;
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
    } catch (error) {
      const jobs = unfinishedJobsFromError(error);
      if (jobs) setPausedJobs(jobs);
      else setMergeError(error instanceof Error ? error.message : String(error));
    }
  };

  return (
    <div className="episode-merge-panel" aria-label="Merge">
      {previewError && (
        <div className="campaign-branch-diagnostic" role="alert">
          {previewError}
        </div>
      )}
      {preview?.code && (
        <div className="merge-code-summary">
          <strong>{preview.code.repo_alias}</strong>
          <span>
            {preview.code.source_branch} → {preview.code.target_branch}
          </span>
          <span>{preview.code.commits_ahead} commits ahead</span>
          {/* Leftovers are committed at Merge, so a tip equal to the target is not merged yet. */}
          {!(preview.code.status === "already_merged" && preview.code.leftover_files.length) && (
            <span className={`status-pill merge-code-${preview.code.status}`}>
              {CODE_STATUS_LABELS[preview.code.status]}
            </span>
          )}
          {preview.code.leftover_files.length > 0 && (
            <span>{preview.code.leftover_files.length} uncommitted files will be committed</span>
          )}
          {preview.code.conflict_files.length > 0 && (
            <ul className="merge-conflict-files" aria-label="Conflicting files">
              {preview.code.conflict_files.map((path) => (
                <li key={path}>{path}</li>
              ))}
            </ul>
          )}
        </div>
      )}
      {counts && hasGraphBranch && (
        <div className="merge-graph-counts" aria-label="Graph changes">
          <span>{counts.changed} changed</span>
          <span>{counts.proposals} Proposals</span>
          <span>{counts.conflicts} conflicts</span>
          <span>{counts.needsAgent} need agent</span>
          {counts.delivered > 0 && <span>{counts.delivered} already merged</span>}
        </div>
      )}
      {episode.isolation_state?.merge_attempt?.worktree_kept && (
        <span className="merge-needs-agent">
          Merged, but the worktree was kept: a job was still running. Merge again once it ends to
          remove it.
        </span>
      )}
      {preview?.needs_agent && (
        <span className="merge-needs-agent">A merge agent will resolve what RCP cannot.</span>
      )}
      {preview && (
        <fieldset className="merge-options" disabled={disabled}>
          {preview.code && (
            <>
              <label>
                <span>Target branch</span>
                <input
                  value={targetDraft}
                  onChange={(event) => setTargetDraft(event.target.value)}
                  onBlur={() => setTarget(targetDraft.trim() || null)}
                />
              </label>
              <label>
                <input
                  type="radio"
                  name={`history-${episode.episode_id}`}
                  checked={historyMode === "merge"}
                  onChange={() => setChoices({ ...choices, historyMode: "merge" })}
                />
                Merge commit
              </label>
              <label>
                <input
                  type="radio"
                  name={`history-${episode.episode_id}`}
                  checked={historyMode === "squash"}
                  disabled={!squash}
                  onChange={() => setChoices({ ...choices, historyMode: "squash" })}
                />
                Squash
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={choices.removeWorktree}
                  onChange={(event) =>
                    setChoices({ ...choices, removeWorktree: event.target.checked })
                  }
                />
                Remove worktree
              </label>
              <label>
                <input
                  type="checkbox"
                  checked={choices.removeWorktree && choices.deleteCodeBranch}
                  disabled={!choices.removeWorktree}
                  onChange={(event) =>
                    setChoices({ ...choices, deleteCodeBranch: event.target.checked })
                  }
                />
                Delete code branch
              </label>
            </>
          )}
          {hasGraphBranch && (
            <label>
              <input
                type="checkbox"
                checked={historyMode !== "squash" && choices.keepBranchOpen}
                disabled={historyMode === "squash"}
                onChange={(event) =>
                  setChoices({ ...choices, keepBranchOpen: event.target.checked })
                }
              />
              Keep branch open
            </label>
          )}
        </fieldset>
      )}
      <button
        className="button primary compact campaign-branch-merge"
        type="button"
        disabled={disabled || !previewCurrent}
        onClick={() => void merge()}
      >
        {busy ? <LoaderCircle className="spin" size={12} /> : <Network size={12} />}
        {busy ? "Starting merge…" : "Merge"}
      </button>
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
      {mergeError && (
        <div className="campaign-branch-diagnostic" role="alert">
          {mergeError}
        </div>
      )}
    </div>
  );
}
