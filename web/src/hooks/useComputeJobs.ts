import { useCallback, useEffect, useRef, useState } from "react";
import { cancelComputeJob, listComputeJobs } from "../api";
import type { ComputeJobRecord, WatcherRecord } from "../types";

/**
 * Refresh with the view's existing watcher updates; no independent timer. Reads are serialized:
 * a refresh requested while one is in flight runs once after it, so a slow reconciliation is
 * never aborted by the next watcher poll.
 */
export function useComputeJobs(apiBase: string, watchers: readonly WatcherRecord[]) {
  const [jobs, setJobs] = useState<ComputeJobRecord[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<string | null>(null);
  const currentBase = useRef(apiBase);
  currentBase.current = apiBase;
  const inFlight = useRef(false);
  const refreshAgain = useRef(false);

  const refresh = useCallback(async () => {
    if (inFlight.current) {
      refreshAgain.current = true;
      return;
    }
    inFlight.current = true;
    try {
      const next = await listComputeJobs(apiBase);
      if (currentBase.current === apiBase) {
        setJobs(next);
        setError(null);
      }
    } catch (caught) {
      if (currentBase.current === apiBase)
        setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      inFlight.current = false;
      if (refreshAgain.current) {
        refreshAgain.current = false;
        if (currentBase.current === apiBase) void refresh();
      }
    }
  }, [apiBase]);

  useEffect(() => {
    setJobs([]);
    setError(null);
    setCancelling(null);
    refreshAgain.current = false;
  }, [apiBase]);

  useEffect(() => {
    void refresh();
  }, [refresh, watchers]);

  const cancel = async (jobId: string) => {
    if (cancelling) return;
    setCancelling(jobId);
    setError(null);
    try {
      const updated = await cancelComputeJob(apiBase, jobId);
      if (currentBase.current !== apiBase) return;
      setJobs((current) => current.map((job) => (job.job_id === updated.job_id ? updated : job)));
      await refresh();
    } catch (caught) {
      if (currentBase.current === apiBase)
        setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      if (currentBase.current === apiBase) setCancelling(null);
    }
  };

  return { jobs, error, cancelling, cancel };
}
