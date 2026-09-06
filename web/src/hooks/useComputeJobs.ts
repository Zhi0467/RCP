import { useEffect, useRef, useState } from "react";
import { cancelComputeJob, listComputeJobs } from "../api";
import type { ComputeJobRecord, WatcherRecord } from "../types";

/** Refresh with the view's existing watcher updates; no independent timer. */
export function useComputeJobs(apiBase: string, watchers: readonly WatcherRecord[]) {
  const [jobs, setJobs] = useState<ComputeJobRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const generation = useRef(0);
  const currentBase = useRef(apiBase);
  currentBase.current = apiBase;

  useEffect(() => {
    setJobs([]);
    setError(null);
    setCancelling(null);
  }, [apiBase]);

  useEffect(() => {
    const request = ++generation.current;
    const controller = new AbortController();
    void listComputeJobs(apiBase, controller.signal)
      .then((next) => {
        if (request === generation.current) {
          setJobs(next);
          setError(null);
        }
      })
      .catch((caught: unknown) => {
        if (!controller.signal.aborted && request === generation.current) {
          setError(caught instanceof Error ? caught.message : String(caught));
        }
      });
    return () => {
      controller.abort();
      generation.current += 1;
    };
  }, [apiBase, watchers]);

  const cancel = async (jobId: string) => {
    if (cancelling) return;
    setCancelling(jobId);
    setError(null);
    // A pre-cancel read cannot overwrite the cancellation response.
    generation.current += 1;
    try {
      const updated = await cancelComputeJob(apiBase, jobId);
      if (currentBase.current !== apiBase) return;
      generation.current += 1;
      setJobs((current) => current.map((job) => (job.job_id === updated.job_id ? updated : job)));
      const request = ++generation.current;
      const next = await listComputeJobs(apiBase);
      if (currentBase.current === apiBase && request === generation.current) setJobs(next);
    } catch (caught) {
      if (currentBase.current === apiBase)
        setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (currentBase.current === apiBase) setCancelling(null);
    }
  };

  return { jobs, error, cancelling, cancel };
}
