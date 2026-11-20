import {
  CalendarClock,
  Hourglass,
  Paperclip,
  Pause,
  Pencil,
  Play,
  RotateCcw,
  TriangleAlert,
  X,
} from "lucide-react";
import { useCallback, useRef, useState } from "react";
import { errorMessage } from "../core/errors";
import { useLayerDismiss } from "../core/useLayerDismiss";
import type { AgentSchedule, AgentTask, AgentThreadQueueItem } from "../core/types";
import {
  firingScheduleCount,
  handoffReport,
  oneShotPassed,
  scheduleInstant,
  scheduleLimits,
  scheduleWhen,
  upcomingSchedules,
} from "../agents/agentThreadModel";
import {
  cancelThreadSchedule,
  enableThreadSchedule,
  setThreadSchedulePaused,
} from "../agents/agentThreadsApi";

const HANDOFF_STATUS: Record<string, string> = {
  succeeded: "Done",
  failed: "Failed",
  refused: "Not started",
  interrupted: "Stopped",
  canceled: "Stopped",
};

/** A handed-off turn's settlement: the agent that ran it and how it ended; its reply stays in that agent's thread. */
export function HandoffResult({
  report,
  agentName,
}: {
  report: NonNullable<ReturnType<typeof handoffReport>>;
  agentName: string;
}) {
  return (
    <div className="handoff-result" data-status={report.status}>
      <div className="handoff-result-row">
        <span className="chat-turn-trigger handoff">Handoff</span>
        <strong>{agentName}</strong>
        <span className="handoff-result-status">
          {HANDOFF_STATUS[report.status] ?? report.status.replaceAll("_", " ")}
        </span>
      </div>
    </div>
  );
}

export type QueuedMessage = Extract<AgentThreadQueueItem, { kind: "message" }>;

/**
 * What waits for the thread, one line each, above the composer. A removed or
 * dismissed line collapses first, then deletes; a failed delete brings it back.
 */
export function ThreadQueue({
  items,
  onRemove,
}: {
  items: readonly AgentThreadQueueItem[];
  /** Deletes the row; a dismissed refusal also returns its text to the draft. */
  onRemove: (item: QueuedMessage, dismissed: boolean) => Promise<void>;
}) {
  const [leaving, setLeaving] = useState<ReadonlyMap<string, boolean>>(() => new Map());
  const [gone, setGone] = useState<ReadonlySet<string>>(() => new Set());
  const shown = items.filter((item) => item.kind === "handoff" || !gone.has(item.queue_id));
  if (!shown.length) return null;
  const forget = (id: string) => {
    setGone((known) => new Set([...known].filter((item) => item !== id)));
    setLeaving((known) => new Map([...known].filter(([item]) => item !== id)));
  };
  const left = (item: QueuedMessage) => {
    setGone((known) => new Set(known).add(item.queue_id));
    onRemove(item, leaving.get(item.queue_id) ?? false).catch(() => forget(item.queue_id));
  };
  return (
    <ol className="thread-queue">
      {shown.map((item) => {
        if (item.kind === "handoff")
          return (
            <li className="thread-queue-row" key={`handoff:${item.handoff_id}`}>
              <span className="chat-turn-trigger handoff">Handoff</span>
              <strong>{item.source_agent_name}</strong>
              <span className="thread-queue-text" title={item.message}>
                {item.message}
              </span>
            </li>
          );
        const reason = item.refusal_reason?.replaceAll("_", " ");
        const files = item.attachment_count ?? 0;
        return (
          <li
            className="thread-queue-row"
            key={item.queue_id}
            data-refused={reason ? "" : undefined}
            data-leaving={leaving.has(item.queue_id) ? "" : undefined}
            onAnimationEnd={(event) => {
              if (
                event.target === event.currentTarget &&
                event.animationName === "thread-queue-leave"
              )
                left(item);
            }}
          >
            {reason ? (
              <TriangleAlert className="thread-queue-mark" size={13} aria-hidden="true" />
            ) : (
              <Hourglass className="thread-queue-mark" size={12} aria-hidden="true" />
            )}
            <span
              className="thread-queue-text"
              title={reason ? `${reason}\n${item.message}` : item.message}
            >
              {reason && <span className="thread-queue-reason">{reason}</span>}
              {item.message}
            </span>
            {files > 0 && (
              <span className="thread-queue-files" aria-label={`${files} files`}>
                <Paperclip size={12} aria-hidden="true" />
                {files}
              </span>
            )}
            {reason ? (
              <button
                className="button compact secondary"
                type="button"
                disabled={leaving.has(item.queue_id)}
                onClick={() => setLeaving((known) => new Map(known).set(item.queue_id, true))}
              >
                Dismiss
              </button>
            ) : (
              <>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Edit"
                  title="Edit"
                  disabled={leaving.has(item.queue_id)}
                  onClick={() => setLeaving((known) => new Map(known).set(item.queue_id, true))}
                >
                  <Pencil size={14} />
                </button>
                <button
                  className="icon-button"
                  type="button"
                  aria-label="Remove"
                  title="Remove"
                  disabled={leaving.has(item.queue_id)}
                  onClick={() => setLeaving((known) => new Map(known).set(item.queue_id, false))}
                >
                  <X size={14} />
                </button>
              </>
            )}
          </li>
        );
      })}
    </ol>
  );
}

export function FreshSessionMarker({
  task,
  hasEarlierTurn,
}: {
  task: AgentTask | undefined;
  hasEarlierTurn: boolean;
}) {
  const resolution = task?.chat_session_resolution;
  if (!hasEarlierTurn || resolution?.outcome !== "fresh") return null;
  return (
    <span
      className="chat-turn-trigger fresh-session"
      title={
        resolution.reason_code === "provider_switch_compacted"
          ? "The provider changed. Earlier conversation was compacted for the new session."
          : resolution.reason_code === "provider_switch_tail"
            ? "The provider changed. Recent conversation was carried into the new session."
            : resolution.reason_code?.replaceAll("_", " ")
      }
    >
      <RotateCcw size={11} aria-hidden="true" />
      Fresh start
    </span>
  );
}

const PAUSED_BECAUSE: Record<NonNullable<AgentSchedule["disabled_reason"]>, string> = {
  restore: "Paused by a restore",
  access_lost: "Paused while you had no access to the project",
  unschedulable: "Paused because its time can no longer be worked out",
};

/** The thread's schedules, behind one Wakes button; each row pauses, resumes, enables or cancels. */
export function SchedulesPopover({
  threadId,
  schedules,
  onChanged,
}: {
  threadId: string;
  schedules: AgentSchedule[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const panel = useRef<HTMLDivElement>(null);
  const close = useCallback(() => setOpen(false), []);
  useLayerDismiss(open, close, [trigger, panel], trigger);
  const upcoming = upcomingSchedules(schedules);
  if (!upcoming.length) return null;
  const firing = firingScheduleCount(upcoming);
  const act = async (run: () => Promise<unknown>) => {
    setError(null);
    try {
      await run();
    } catch (failure) {
      setError(errorMessage(failure));
    }
    onChanged();
  };
  return (
    <>
      <button
        ref={trigger}
        className="upcoming-button"
        type="button"
        title="Wakes"
        aria-label={firing ? `${firing} wakes` : "Wakes"}
        aria-expanded={open}
        onClick={() => setOpen(!open)}
      >
        <CalendarClock size={14} aria-hidden="true" />
        {firing > 0 && firing}
      </button>
      {open && (
        <div className="upcoming-popover" role="dialog" aria-label="Wakes" ref={panel}>
          <h3>Wakes</h3>
          {upcoming.map((schedule) => {
            const cancel = () =>
              void act(() => cancelThreadSchedule(threadId, schedule.schedule_id));
            const paused = schedule.state === "paused";
            /** A one-shot whose time passed while paused has no run left to resume. */
            const resumable = paused && !oneShotPassed(schedule);
            const next = schedule.state === "active" && !schedule.at_local && schedule.next_fire_at;
            return (
              <div
                className="upcoming-row"
                data-state={schedule.state}
                key={schedule.schedule_id}
                title={
                  schedule.disabled_reason ? PAUSED_BECAUSE[schedule.disabled_reason] : undefined
                }
              >
                <strong title={schedule.note}>{schedule.note}</strong>
                <span className="upcoming-when">
                  <time>{[scheduleWhen(schedule), ...scheduleLimits(schedule)].join(" · ")}</time>
                  <span className="upcoming-zone">{schedule.timezone}</span>
                  {next && (
                    <time className="upcoming-next" dateTime={next} title="Next run">
                      → {scheduleInstant(next)}
                    </time>
                  )}
                </span>
                {schedule.pending_since && (
                  <span
                    className="upcoming-pending"
                    data-deferred={schedule.deferred_reason ? "" : undefined}
                    title={
                      schedule.deferred_reason ? undefined : "Starts when the current turn ends"
                    }
                  >
                    <Hourglass size={11} aria-hidden="true" />
                    <time dateTime={schedule.pending_since}>
                      {scheduleInstant(schedule.pending_since)}
                    </time>
                    {schedule.deferred_reason && <span>{schedule.deferred_reason}</span>}
                  </span>
                )}
                <span className="upcoming-actions">
                  {schedule.state === "disabled" && !oneShotPassed(schedule) && (
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={`Enable ${schedule.note}`}
                      title="Enable"
                      onClick={() =>
                        void act(() => enableThreadSchedule(threadId, schedule.schedule_id))
                      }
                    >
                      <Play size={14} />
                    </button>
                  )}
                  {(resumable || schedule.state === "active") && (
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={`${paused ? "Resume" : "Pause"} ${schedule.note}`}
                      title={paused ? "Resume" : "Pause"}
                      onClick={() =>
                        void act(() =>
                          setThreadSchedulePaused(threadId, schedule.schedule_id, !paused),
                        )
                      }
                    >
                      {paused ? <Play size={14} /> : <Pause size={14} />}
                    </button>
                  )}
                  <button
                    className="icon-button"
                    type="button"
                    aria-label={`Cancel ${schedule.note}`}
                    title="Cancel"
                    onClick={cancel}
                  >
                    <X size={14} />
                  </button>
                </span>
              </div>
            );
          })}
          {error && (
            <p className="upcoming-error" role="alert">
              {error}
            </p>
          )}
        </div>
      )}
    </>
  );
}
