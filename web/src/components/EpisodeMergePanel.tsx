import { LoaderCircle, Network } from "lucide-react";
import { useEffect, useState } from "react";
import { loadMergePreview } from "../api";
import { type MergeChoices, mergeDiffCounts, mergeRequestBody, squashAllowed } from "../mergePanel";
import type { Episode, MergeEpisodeBody, MergePreview } from "../types";

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
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [mergeError, setMergeError] = useState<string | null>(null);
  const [targetDraft, setTargetDraft] = useState("");
  const [target, setTarget] = useState<string | null>(null);
  const [choices, setChoices] = useState<MergeChoices>({
    targetBranch: "",
    historyMode: "merge",
    keepBranchOpen: false,
    removeWorktree: true,
    deleteCodeBranch: true,
  });
  // A new branch head, attempt, or target means a new preview.
  const refreshKey = JSON.stringify([
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
    loadMergePreview(apiBase, episode.episode_id, target)
      .then((next) => {
        if (!current) return;
        setPreview(next);
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

  const merge = async () => {
    if (!preview || disabled) return;
    setMergeError(null);
    try {
      await onMerge(
        episode.episode_id,
        mergeRequestBody(preview, { ...choices, targetBranch: targetDraft }, hasGraphBranch),
      );
    } catch (error) {
      setMergeError(error instanceof Error ? error.message : String(error));
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
        disabled={disabled || !preview}
        onClick={() => void merge()}
      >
        {busy ? <LoaderCircle className="spin" size={12} /> : <Network size={12} />}
        {busy ? "Starting merge…" : "Merge"}
      </button>
      {mergeError && (
        <div className="campaign-branch-diagnostic" role="alert">
          {mergeError}
        </div>
      )}
    </div>
  );
}
