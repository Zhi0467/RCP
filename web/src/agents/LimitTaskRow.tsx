import { RotateCcw } from "lucide-react";
import { useState } from "react";
import type { AgentTask, LimitReset } from "../core/types";
import { askedLabel } from "./agentThreadModel";
import { TaskActionButton } from "./taskActions";

const STOP_REASONS: Record<string, string> = {
  declined: "Don't resume was pressed",
  switch_off: "Resume after usage limits is off",
};

function limitText(task: AgentTask, limit: LimitReset): string {
  const provider = task.provider_label;
  if (limit.state === "waiting")
    return limit.estimated
      ? `${provider} usage limit. Resumes later.`
      : `${provider} usage limit. Resumes at ${askedLabel(limit.resets_at, new Date())}.`;
  return limit.estimated
    ? `${provider} usage limit.`
    : `${provider} usage limit until ${askedLabel(limit.resets_at, new Date())}.`;
}

/** A turn stopped by a provider usage limit: one line, its action at the end. */
export function LimitTaskRow({
  task,
  limit,
  disabled,
  onResume,
  onRetry,
  onDecline,
}: {
  task: AgentTask;
  limit: LimitReset;
  disabled: boolean;
  onResume: () => void;
  onRetry: () => void;
  onDecline: () => Promise<void>;
}) {
  const [declining, setDeclining] = useState(false);
  const reason =
    limit.state === "stopped" && limit.reason
      ? (STOP_REASONS[limit.reason] ?? limit.reason)
      : undefined;
  return (
    <div
      className="chat-task-inline paused chat-limit-row"
      role="status"
      data-task-id={task.operation_id}
      data-limit-state={limit.state}
    >
      <span title={reason}>{limitText(task, limit)}</span>
      {limit.state === "waiting" ? (
        <button
          type="button"
          className="button compact"
          data-limit-action="decline"
          disabled={disabled || declining}
          onClick={() => {
            setDeclining(true);
            void onDecline().finally(() => setDeclining(false));
          }}
        >
          Don't resume
        </button>
      ) : (
        <>
          {task.can_resume && (
            <TaskActionButton
              action="resume"
              className="button compact primary"
              disabled={disabled}
              onClick={onResume}
            />
          )}
          {task.can_retry && (
            <button
              type="button"
              className="icon-button chat-error-retry"
              aria-label="Retry"
              title="Retry"
              data-task-action="retry"
              disabled={disabled}
              onClick={onRetry}
            >
              <RotateCcw size={14} />
            </button>
          )}
        </>
      )}
    </div>
  );
}
