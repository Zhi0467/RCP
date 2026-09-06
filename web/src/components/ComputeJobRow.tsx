import { computeJobPresentation } from "../compute";
import type { ComputeJobRecord } from "../types";

export function ComputeJobRow({
  jobId,
  jobs,
}: {
  jobId: string;
  jobs: readonly ComputeJobRecord[];
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
    </>
  );
}
