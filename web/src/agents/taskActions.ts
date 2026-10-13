import type { ResourceStore } from "../core/resourceStore";
import type { AgentTask } from "../core/types";
import { tasksResourceKey } from "./taskResources";

export type TaskAction = "pause" | "resume" | "retry" | "stop";

/** Task commands share one fenced list and paint their intent before network I/O. */
export function beginTaskMutation(
  store: ResourceStore,
  projectId: string,
  task: AgentTask,
  action: TaskAction,
) {
  const id = tasksResourceKey(projectId);
  const operationId = `${task.operation_id}:${action}:${crypto.randomUUID()}`;
  const pending = (value: unknown) =>
    ((value as AgentTask[] | undefined) ?? [task]).map((row) => {
      if (row.operation_id !== task.operation_id) return row;
      return {
        ...row,
        action_error: undefined,
        action_error_action: undefined,
        action_pending: action,
        status_label: { pause: "Pausing", resume: "Resuming", retry: "Retrying", stop: "Stopping" }[
          action
        ],
        ...(action === "pause" || action === "stop"
          ? { pausing: true, can_pause: false }
          : { queued: true, paused: false, can_resume: false, can_retry: false }),
      };
    });
  const mutation = store.beginMutation(operationId, new Map([[id, pending]]));
  return {
    settle(next?: AgentTask) {
      if (!next) {
        mutation.settle();
        return;
      }
      const rows = store.committedValue<AgentTask[]>(id) ?? [];
      mutation.settle(
        new Map([
          [
            id,
            [
              next,
              ...rows
                .filter((row) => row.operation_id !== next.operation_id)
                .map((row) =>
                  row.operation_id === task.operation_id
                    ? {
                        ...row,
                        action_error: undefined,
                        action_error_action: undefined,
                        action_pending: undefined,
                      }
                    : row,
                ),
            ],
          ],
        ]),
      );
    },
    fail(error: unknown) {
      mutation.fail();
      const rows = store.committedValue<AgentTask[]>(id) ?? [];
      const message = error instanceof Error ? error.message : String(error);
      store.write(
        id,
        rows.map((row) =>
          row.operation_id === task.operation_id
            ? {
                ...row,
                action_error: message,
                action_error_action: action,
                action_pending: undefined,
              }
            : row,
        ),
      );
    },
  };
}
