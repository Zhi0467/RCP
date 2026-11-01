import { useCallback, useEffect, useState } from "react";
import { errorMessage } from "../core/errors";
import type { AgentThread } from "../core/types";
import { loadAgentThreads } from "./agentThreadsApi";

/** The rail follows new replies, wakes and live state at this pace. */
const THREAD_LIST_POLL_MS = 10_000;

/** The member's threads, refreshed on a timer; an edit patches its row in place. */
export function useAgentThreads(enabled = true) {
  const [threads, setThreads] = useState<AgentThread[] | null>(null);
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
    void reload();
    const timer = window.setInterval(() => void reload(), THREAD_LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [enabled, reload]);
  const patch = useCallback((thread: AgentThread) => {
    setThreads(
      (current) =>
        current?.map((item) => (item.thread_id === thread.thread_id ? thread : item)) ?? current,
    );
  }, []);
  return { threads, error, reload, patch };
}
