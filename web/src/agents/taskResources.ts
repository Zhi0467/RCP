import { api } from "../core/api";
import { resourceKeyId, type ResourceKey, type ResourceProfile } from "../core/resourceStore";
import type { AgentTask, AgentUsageSnapshot } from "../core/types";

// The containing runtime is recreated for each backend/member identity.
function key(project: string, name: string, entity?: string): ResourceKey {
  return { backend: "", member: "", project, name, ...(entity ? { entity } : {}) };
}

export function tasksResourceKey(projectId: string): string {
  return resourceKeyId(key(projectId, "tasks.status"));
}

export function taskStatusProfile(projectId: string): ResourceProfile<AgentTask[]> {
  return {
    watch: [{ name: "chats" }],
    key: key(projectId, "tasks.status"),
    fetch: (signal) => api(`/api/projects/${encodeURIComponent(projectId)}/tasks`, { signal }),
  };
}

export function taskProgressProfile(projectId: string): ResourceProfile<AgentTask[]> {
  return { ...taskStatusProfile(projectId), key: key(projectId, "tasks.progress") };
}

export function taskResourceProfile(
  projectId: string,
  operationId: string,
): ResourceProfile<AgentTask> {
  return {
    watch: [{ name: "chats" }],
    key: key(projectId, "task", operationId),
    fetch: (signal) =>
      api(
        `/api/projects/${encodeURIComponent(projectId)}/tasks/${encodeURIComponent(operationId)}`,
        { signal },
      ),
  };
}

export function usageProfile(projectId: string): ResourceProfile<AgentUsageSnapshot> {
  return {
    key: key(projectId, "usage"),
    fetch: (signal) => api(`/api/projects/${encodeURIComponent(projectId)}/usage`, { signal }),
  };
}

/**
 * Status owns list membership; a progress row at least as new replaces its row.
 * Live fields such as steering, status message, and session move only the
 * progress watermark while a task runs. An equal timestamp still replaces:
 * elapsed time and progress move with the clock while the persisted row does not.
 * A pending or refused command owns its row's controls, so only the clock fields
 * enrich it until the command settles.
 */
export function selectTasks(status: AgentTask[] = [], progress: AgentTask[] = []): AgentTask[] {
  const byId = new Map(progress.map((task) => [task.operation_id, task]));
  return status.map((task) => {
    const detail = byId.get(task.operation_id);
    if (!detail || detail.status !== task.status || detail.updated_at < task.updated_at)
      return task;
    if (!task.action_pending && !task.action_error) return detail;
    return {
      ...task,
      phase: detail.phase,
      progress: detail.progress,
      elapsed_seconds: detail.elapsed_seconds,
      last_activity_at: detail.last_activity_at,
    };
  });
}
