import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../core/errors";
import { retryBackoffMs } from "../core/resourceStore";
import type { AgentThread } from "../core/types";
import { useRefreshRuntime } from "../core/useResource";
import { loadAgentThreads } from "./agentThreadsApi";

/**
 * The member's threads. The refresh scheduler reads the `agent_threads`
 * watermark, which moves on any turn, read, rename, schedule or question; each
 * move reloads the list, and `revision` tells the open thread to reload too.
 * A failed list read backs off and retries at the same version.
 */
export function useAgentThreads(enabled = true) {
  const { scheduler } = useRefreshRuntime();
  const [threads, setThreads] = useState<AgentThread[] | null>(null);
  const [revision, setRevision] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      setThreads((await loadAgentThreads()).threads);
      setError(null);
      return true;
    } catch (failure) {
      setError(errorMessage(failure));
      return false;
    }
  }, []);
  useEffect(() => {
    if (!enabled) return;
    let stopped = false;
    let loading = false;
    let failures = 0;
    let retryAt = 0;
    // Undefined until the first read, so the first answer always loads the list.
    let seen: string | null | undefined;
    const observe = (version: string | null) => {
      if (stopped || loading || version === seen || Date.now() < retryAt) return;
      loading = true;
      void reload().then((loaded) => {
        loading = false;
        if (stopped) return;
        // A version counts as seen only once its list loaded.
        if (loaded) {
          failures = 0;
          retryAt = 0;
          seen = version;
          setRevision(version);
        } else {
          failures += 1;
          retryAt = Date.now() + retryBackoffMs(failures);
        }
      });
    };
    const unwatch = scheduler.watchAgentThreads(observe);
    return () => {
      stopped = true;
      unwatch();
    };
  }, [enabled, reload, scheduler]);
  const patch = useCallback((thread: AgentThread) => {
    setThreads(
      (current) =>
        current?.map((item) => (item.thread_id === thread.thread_id ? thread : item)) ?? current,
    );
  }, []);
  return { threads, revision, error, reload, patch };
}
