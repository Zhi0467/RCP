import { LoaderCircle, Play, RotateCcw, Square, type LucideIcon } from "lucide-react";
import { createElement } from "react";
import { ApiError } from "../core/api";
import { type ResourceStore, resourceKeyId } from "../core/resourceStore";
import type { AgentTask } from "../core/types";
import { taskResourceProfile, tasksResourceKey } from "./taskResources";

export type TaskAction = "pause" | "resume" | "retry" | "stop";

/** How every surface names and draws one turn control. Pausing a turn reads Stop. */
export const TASK_ACTIONS: Record<
  TaskAction,
  { label: string; busyLabel: string; pendingLabel: string; icon: LucideIcon }
> = {
  pause: { label: "Stop", busyLabel: "Stopping…", pendingLabel: "Pausing", icon: Square },
  resume: { label: "Resume", busyLabel: "Resuming…", pendingLabel: "Resuming", icon: Play },
  retry: { label: "Retry", busyLabel: "Retrying…", pendingLabel: "Retrying", icon: RotateCcw },
  stop: { label: "Stop", busyLabel: "Stopping…", pendingLabel: "Stopping", icon: Square },
};

/** One turn control as a menu row. */
export function taskActionItem(
  action: TaskAction,
  item: { label?: string; disabled?: boolean; onSelect: () => void },
) {
  return { label: TASK_ACTIONS[action].label, icon: TASK_ACTIONS[action].icon, ...item };
}

/** One turn control as a button, drawn the same wherever a turn can be controlled. */
export function TaskActionButton({
  action,
  label,
  busy = false,
  disabled = false,
  className = "button compact secondary",
  size = 12,
  onClick,
}: {
  action: TaskAction;
  label?: string;
  busy?: boolean;
  disabled?: boolean;
  className?: string;
  size?: number;
  onClick: () => void;
}) {
  const { icon, busyLabel, label: named } = TASK_ACTIONS[action];
  return createElement(
    "button",
    {
      type: "button",
      className,
      disabled,
      "aria-busy": busy,
      "data-task-action": action,
      onClick,
    },
    busy ? createElement(LoaderCircle, { className: "spin", size }) : createElement(icon, { size }),
    " ",
    busy ? busyLabel : (label ?? named),
  );
}

/** Task commands share one fenced list and paint their intent before network I/O. */
export function beginTaskMutation(
  store: ResourceStore,
  projectId: string,
  task: AgentTask,
  action: TaskAction,
) {
  const id = tasksResourceKey(projectId);
  const operationId = `${task.operation_id}:${action}:${crypto.randomUUID()}`;
  const feedbackId = `task-action:${task.operation_id}`;
  store.clearOverlay(id, feedbackId);
  const pending = (value: unknown) =>
    ((value as AgentTask[] | undefined) ?? [task]).map((row) => {
      if (row.operation_id !== task.operation_id) return row;
      return {
        ...row,
        action_error: undefined,
        action_error_action: undefined,
        action_pending: action,
        status_label: TASK_ACTIONS[action].pendingLabel,
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
      // A missing answer is not a rejection: the server may have admitted the
      // action, so reread the task instead of showing it failed. The caller
      // keeps the admission key, so a repeat cannot start a second recovery.
      if (!(error instanceof ApiError) || error.status >= 500) {
        mutation.settle();
        return;
      }
      const message = error instanceof Error ? error.message : String(error);
      mutation.fail(
        new Map([
          [
            id,
            {
              id: feedbackId,
              apply: (value) =>
                ((value as AgentTask[] | undefined) ?? []).map((row) =>
                  row.operation_id === task.operation_id
                    ? { ...row, action_error: message, action_error_action: action }
                    : row,
                ),
            },
          ],
        ]),
      );
    },
  };
}

export function beginTaskPublication(store: ResourceStore, projectId: string, taskId?: string) {
  const id = tasksResourceKey(projectId);
  const detailId = taskId ? resourceKeyId(taskResourceProfile(projectId, taskId).key) : null;
  const mutation = store.beginMutation(
    crypto.randomUUID(),
    new Map((detailId ? [id, detailId] : [id]).map((key) => [key, (value: unknown) => value])),
  );
  return {
    settle(task?: AgentTask) {
      if (!task) return mutation.settle();
      const rows = store.committedValue<AgentTask[]>(id) ?? [];
      const values = new Map<string, unknown>([
        [id, [task, ...rows.filter((row) => row.operation_id !== task.operation_id)]],
      ]);
      if (detailId && task.operation_id === taskId) values.set(detailId, task);
      mutation.settle(values);
    },
    fail() {
      mutation.fail();
    },
  };
}
