import { computeJobPresentation } from "../compute";
import type { ComputeJobRecord } from "../types";

export function ComputeJobRow({
  jobId,
  jobs,
  onCancel,
  cancelling = null,
}: {
  jobId: string;
  jobs: readonly ComputeJobRecord[];
  /** The backend's can_cancel decides visibility; the cancel route enforces write admission. */
  onCancel?: (jobId: string) => void;
  cancelling?: string | null;
}) {
  const job = jobs.find((item) => item.job_id === jobId);
  if (!job)
    return (
      <>
        <strong>{jobId}</strong>
        <span>Job details unavailable</span>
      </>
    );
  const presentation = computeJobPresentation(job);
  return (
    <>
      <strong>{presentation.label}</strong>
      <span>{presentation.status}</span>
      <span>{presentation.exitStatus}</span>
      <span>{presentation.backend}</span>
      {presentation.cancellation && <span>{presentation.cancellation}</span>}
      {job.diagnostic && <span role="alert">{job.diagnostic}</span>}
      {job.can_cancel && onCancel && (
        <button
          type="button"
          className="button compact"
          onClick={() => onCancel(job.job_id)}
          disabled={cancelling !== null}
          aria-label={`Cancel compute job ${presentation.label}`}
        >
          {cancelling === job.job_id ? "Cancelling…" : "Cancel"}
        </button>
      )}
    </>
  );
}
