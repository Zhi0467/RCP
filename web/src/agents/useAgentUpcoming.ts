import { useCallback, useEffect, useState } from "react";
import type { AgentSchedule, AgentThreadQueueItem } from "../core/types";
import { errorMessage } from "../core/errors";
import { loadThreadQueue, loadThreadSchedules } from "./agentThreadsApi";

export function useAgentUpcoming(agentId: string | null, revision: string | null) {
  const [state, setState] = useState<{
    agentId: string;
    queue: AgentThreadQueueItem[];
    schedules: AgentSchedule[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    if (!agentId) return;
    try {
      const [queue, schedules] = await Promise.all([
        loadThreadQueue({ thread_id: agentId }),
        loadThreadSchedules(agentId),
      ]);
      setState({ agentId, queue: queue.items, schedules: schedules.schedules });
      setError(null);
    } catch (failure) {
      setError(errorMessage(failure));
    }
  }, [agentId]);
  useEffect(() => {
    void refresh();
  }, [refresh, revision]);
  return { state: state?.agentId === agentId ? state : null, error, refresh };
}
