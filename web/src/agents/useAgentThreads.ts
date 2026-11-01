import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../core/errors";
import { WATERMARK_REQUEST_TIMEOUT_MS } from "../core/refreshScheduler";
import { retryBackoffMs } from "../core/resourceStore";
import type { AgentThread } from "../core/types";
import { loadAgentThreads, loadAgentThreadsVersion } from "./agentThreadsApi";

/** How often the member's agent-thread watermark is read while the page shows. */
const THREAD_WATERMARK_POLL_MS = 2_000;

/**
 * The member's threads. The `agent_threads` watermark moves on any turn, read,
 * rename, schedule or question; each move reloads the list, and `revision`
 * tells the open thread to reload too. Failures back off like the scheduler.
 */
export function useAgentThreads(enabled = true) {
  const [threads, setThreads] = useState<AgentThread[] | null>(null);
  const [revision, setRevision] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      setThreads((await loadAgentThreads()).threads);
      setError(null);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0;
    // Undefined until the first read, so the first answer always loads the list.
    let seen: string | null | undefined;
    const beat = async () => {
      if (document.visibilityState !== "hidden") {
        try {
          const version = await loadAgentThreadsVersion(WATERMARK_REQUEST_TIMEOUT_MS);
          failures = 0;
          if (!stopped && version !== seen) {
            seen = version;
            await reload();
            if (!stopped) setRevision(version);
          }
        } catch {
          failures += 1;
        }
      }
      if (!stopped)
        timer = setTimeout(
          () => void beat(),
          failures ? retryBackoffMs(failures) : THREAD_WATERMARK_POLL_MS,
        );
    };
    void beat();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [enabled, reload]);
  const patch = useCallback((thread: AgentThread) => {
    setThreads(
      (current) =>
        current?.map((item) => (item.thread_id === thread.thread_id ? thread : item)) ?? current,
    );
  }, []);
  return { threads, revision, error, reload, patch };
}
