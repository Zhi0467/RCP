import type { AgentTask } from "./types";

export interface AgentTaskGroup {
  rootId: string;
  root: AgentTask;
  latest: AgentTask;
  attempts: AgentTask[];
}

export interface RunTaskProjection {
  actionable: AgentTaskGroup[];
  running: AgentTaskGroup[];
  completed: AgentTaskGroup[];
}

const actionableStatuses = new Set(["failed", "paused", "interrupted"]);
const runningStatuses = new Set(["queued", "running", "pausing"]);

export function buildRunTaskProjection(tasks: AgentTask[]): RunTaskProjection {
  const groups = groupAgentTasks(tasks);
  return {
    actionable: groups.filter((group) => actionableStatuses.has(group.latest.status)),
    running: groups.filter((group) => runningStatuses.has(group.latest.status)),
    completed: groups.filter((group) => group.latest.status === "succeeded"),
  };
}

export function latestRunObservation(lastRefreshAt: string | null | undefined, tasks: AgentTask[]): string | null {
  const timestamps = [lastRefreshAt, ...tasks.map((task) => task.updated_at)]
    .filter((value): value is string => typeof value === "string" && Number.isFinite(Date.parse(value)))
    .sort((left, right) => Date.parse(right) - Date.parse(left));
  return timestamps[0] ?? null;
}

export function groupAgentTasks(tasks: AgentTask[]): AgentTaskGroup[] {
  const byId = new Map(tasks.map((task) => [task.operation_id, task]));
  const grouped = new Map<string, AgentTask[]>();
  tasks.forEach((task) => {
    const rootId = logicalRootId(task, byId);
    const attempts = grouped.get(rootId) ?? [];
    attempts.push(task);
    grouped.set(rootId, attempts);
  });
  return [...grouped.entries()]
    .map(([rootId, attempts]) => {
      attempts.sort(compareTaskAscending);
      return {
        rootId,
        root: byId.get(rootId) ?? attempts[0],
        latest: attempts.at(-1) ?? attempts[0],
        attempts,
      };
    })
    .sort((left, right) => compareTaskAscending(right.latest, left.latest));
}

function logicalRootId(task: AgentTask, byId: Map<string, AgentTask>): string {
  const seen = new Set([task.operation_id]);
  let current = task;
  while (current.parent_operation_id && byId.has(current.parent_operation_id) && !seen.has(current.parent_operation_id)) {
    seen.add(current.parent_operation_id);
    current = byId.get(current.parent_operation_id) as AgentTask;
  }
  return current.operation_id;
}

function compareTaskAscending(left: AgentTask, right: AgentTask): number {
  return left.created_at.localeCompare(right.created_at) || left.operation_id.localeCompare(right.operation_id);
}
