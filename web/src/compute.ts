import type { AgentTask, ChatMessage, ComputeConnection, ComputeConnectionProbe } from "./types";

export function reconcileActiveComputeIds(
  ids: readonly string[],
  connections: readonly ComputeConnection[],
): string[] {
  const available = new Set(connections.map((connection) => connection.id));
  return ids.filter((id, index) => available.has(id) && ids.indexOf(id) === index);
}

export function latestPersistedComputeIds(
  messages: readonly ChatMessage[],
  tasks: readonly AgentTask[],
  connections: readonly ComputeConnection[],
): string[] {
  const candidates: Array<{ ids: string[]; timestamp: number; order: number }> = [];
  let order = 0;
  messages.forEach((message) => {
    if (Array.isArray(message.active_compute_ids)) {
      candidates.push({
        ids: message.active_compute_ids.filter((id): id is string => typeof id === "string"),
        timestamp: comparableTime(message.timestamp),
        order,
      });
    }
    order += 1;
  });
  tasks.forEach((task) => {
    const ids = task.request.active_compute_ids;
    if (Array.isArray(ids)) {
      candidates.push({
        ids: ids.filter((id): id is string => typeof id === "string"),
        timestamp: comparableTime(task.created_at),
        order,
      });
    }
    order += 1;
  });
  candidates.sort((left, right) => left.timestamp - right.timestamp || left.order - right.order);
  return reconcileActiveComputeIds(candidates.at(-1)?.ids ?? [], connections);
}

export function computeProbePresentation(probe: ComputeConnectionProbe | undefined): {
  label: string;
  tone: "ready" | "error" | "pending";
} {
  if (!probe) return { label: "Not probed", tone: "pending" };
  return { label: probe.status_label, tone: probe.status_tone };
}

function comparableTime(value: string): number {
  const timestamp = Date.parse(value);
  return Number.isFinite(timestamp) ? timestamp : 0;
}
