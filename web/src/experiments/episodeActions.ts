import { useCallback, useRef } from "react";
import { api, ApiError, continueEpisode, stopEpisode } from "../core/api";
import { useRefreshRuntime } from "../core/useResource";
import { isActiveTask } from "../agents/agentTasks";
import { taskControlHeaders } from "../agents/agentThreadsApi";
import { beginTaskMutation } from "../agents/taskActions";
import type { AgentTask, Episode } from "../core/types";

export type EpisodeTaskAction = "pause" | "resume" | "retry";

/**
 * The episode controls Runs and the chat dock share. Each paints its intent on the
 * project's task list before the request, keeps one request id per admission across
 * retries, and returns the server's record; callers own busy state and what follows.
 */
export function useEpisodeActions(projectId: string | null) {
  const { store, scheduler } = useRefreshRuntime();
  const taskRequestIds = useRef(new Map<string, string>());
  const continuationRequestIds = useRef(new Map<string, string>());
  const apiBase = projectId ? `/api/projects/${encodeURIComponent(projectId)}` : null;

  const operateTask = useCallback(
    async (task: AgentTask, action: EpisodeTaskAction): Promise<AgentTask> => {
      if (!apiBase || !projectId) throw new Error("No RCP project is open.");
      const mutation = beginTaskMutation(store, projectId, task, action);
      const admissionKey = JSON.stringify([projectId, task.operation_id, action]);
      const requestId = taskRequestIds.current.get(admissionKey) ?? crypto.randomUUID();
      taskRequestIds.current.set(admissionKey, requestId);
      try {
        const next = await api<AgentTask>(`${apiBase}/tasks/${task.operation_id}/${action}`, {
          method: "POST",
          headers: taskControlHeaders(action, requestId),
        });
        taskRequestIds.current.delete(admissionKey);
        mutation.settle(next);
        scheduler.now();
        return next;
      } catch (error) {
        if (error instanceof ApiError && error.status < 500)
          taskRequestIds.current.delete(admissionKey);
        mutation.fail(error);
        throw error;
      }
    },
    [apiBase, projectId, scheduler, store],
  );

  const stop = useCallback(
    async (episodeId: string, tasks: readonly AgentTask[]): Promise<Episode> => {
      if (!apiBase || !projectId) throw new Error("No RCP project is open.");
      const stopping = tasks
        .filter((task) => task.episode_id === episodeId && isActiveTask(task))
        .map((task) => beginTaskMutation(store, projectId, task, "stop"));
      try {
        const next = await stopEpisode(apiBase, episodeId);
        for (const mutation of stopping) mutation.settle();
        scheduler.now();
        return next;
      } catch (error) {
        for (const mutation of stopping) mutation.fail(error);
        throw error;
      }
    },
    [apiBase, projectId, scheduler, store],
  );

  const addTurns = useCallback(
    async (episodeId: string, invocationCeiling: number): Promise<Episode> => {
      if (!apiBase) throw new Error("No RCP project is open.");
      const requestId = continuationRequestIds.current.get(episodeId) ?? crypto.randomUUID();
      continuationRequestIds.current.set(episodeId, requestId);
      const next = await continueEpisode(apiBase, episodeId, invocationCeiling, requestId);
      continuationRequestIds.current.delete(episodeId);
      scheduler.now();
      return next;
    },
    [apiBase, scheduler],
  );

  return { operateTask, stop, addTurns };
}
