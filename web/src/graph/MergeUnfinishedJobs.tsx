import type { EpisodeUnfinishedJob } from "../core/types";

/** Both human merge entry points require the same explicit job-stop consent. */
export function MergeUnfinishedJobs({
  jobs,
  disabled,
  onConfirm,
  onCancel,
}: {
  jobs: EpisodeUnfinishedJob[];
  disabled: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <div className="merge-unfinished-jobs" role="alert">
      <strong>These jobs may still be writing the worktree.</strong>
      <ul aria-label="Unfinished jobs">
        {jobs.map((job) => (
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
          disabled={disabled}
          onClick={onConfirm}
        >
          Merge anyway
        </button>
        <button className="button secondary compact" type="button" onClick={onCancel}>
          Cancel
        </button>
      </div>
    </div>
  );
}
