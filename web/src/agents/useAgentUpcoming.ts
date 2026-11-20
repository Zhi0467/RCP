import { useCallback, useEffect, useState } from "react";
import type { AgentCapabilities, AgentSchedule, AgentThreadQueueItem } from "../core/types";
import { errorMessage } from "../core/errors";
import { loadAgentCapabilities, loadThreadQueue, loadThreadSchedules } from "./agentThreadsApi";

export function useAgentUpcoming(agentId: string | null, revision: string | null) {
  const [state, setState] = useState<{
    agentId: string;
    capabilities: AgentCapabilities;
    queue: AgentThreadQueueItem[] | null;
    schedules: AgentSchedule[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    if (!agentId) return;
    try {
      const capabilities = await loadAgentCapabilities(agentId);
      const [queue, schedules] = await Promise.all([
        capabilities.can_queue ? loadThreadQueue({ thread_id: agentId }) : null,
        capabilities.can_schedule ? loadThreadSchedules(agentId) : null,
      ]);
      setState({
        agentId,
        capabilities,
        queue: queue?.items ?? null,
        schedules: schedules?.schedules ?? [],
      });
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
