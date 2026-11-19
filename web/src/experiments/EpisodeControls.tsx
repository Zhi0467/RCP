import { LoaderCircle, Pause, Play, RotateCcw, Square } from "lucide-react";
import type { AgentTask, Episode, ExperimentControlState } from "../core/types";
import type { EpisodeProjection } from "./campaigns";
import { authorizedInvocationCount, episodeControlProjection } from "./runProjection";
import type { EpisodeTaskAction } from "./episodeActions";

/**
 * An episode's run controls as its projection allows them: the current task's
 * Pause, Resume or Retry, Add turns, and Stop. Runs and the chat dock both show it.
 */
export function EpisodeControls({
  episode,
  taskControl,
  busyAction,
  taskActionId,
  onOperateTask,
  onSwitchProvider,
  onContinue,
  onStop,
  onError,
  additionalTurns,
  onAdditionalTurnsChange,
  experimentControl,
  disabled = false,
  continuationDisabled = false,
}: {
  episode: Episode | null;
  experimentControl?: ExperimentControlState | null;
  additionalTurns: string;
  onAdditionalTurnsChange: (value: string) => void;
  disabled?: boolean;
  continuationDisabled?: boolean;
  taskControl: EpisodeProjection["taskControl"];
  /** The episode action in flight, as `stop:<episode id>` or `continue:<episode id>`. */
  busyAction: string | null;
  taskActionId: string | null;
  onOperateTask: (task: AgentTask, action: EpisodeTaskAction) => Promise<void>;
  onSwitchProvider?: (task: AgentTask) => void;
  onContinue?: (episodeId: string, invocationCeiling: number) => Promise<void>;
  onStop: (episodeId: string) => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const controls = episodeControlProjection(episode, taskControl, experimentControl);
  taskControl = controls.taskControl;
  const episodeId = episode?.episode_id ?? experimentControl?.episode_id ?? "";
  const parsedAdditionalTurns = authorizedInvocationCount(additionalTurns);
  const turns = controls.resumeTurns ?? parsedAdditionalTurns;
  const stopBusy = busyAction === `stop:${episodeId}`;
  const continueBusy = busyAction === `continue:${episodeId}`;
  const controlTaskBusy = taskControl !== null && taskActionId === taskControl.task.operation_id;
  const anotherActionBusy = disabled || busyAction !== null || taskActionId !== null;
  const continuationBlocked =
    anotherActionBusy || continuationDisabled || !controls.continuationReady;
  const showStop = controls.canStop || stopBusy;
  const report = (run: () => Promise<void>) => {
    onError(null);
    return run().catch((error: unknown) =>
      onError(error instanceof Error ? error.message : String(error)),
    );
  };

  const submitContinuation = async () => {
    if (turns === null || continuationBlocked || !onContinue) return;
    await report(async () => {
      await onContinue(episodeId, turns);
      onAdditionalTurnsChange("");
    });
  };

  return (
    <>
      {taskControl && (
        <button
          className={
            experimentControl
              ? "button primary compact experiment-recovery-button"
              : `button compact ${taskControl.kind === "resume" ? "primary" : "secondary"}`
          }
          type="button"
          disabled={anotherActionBusy}
          aria-busy={controlTaskBusy}
          onClick={() => {
            if (!anotherActionBusy)
              void report(() => onOperateTask(taskControl.task, taskControl.kind));
          }}
        >
          {controlTaskBusy ? (
            <LoaderCircle className="spin" size={12} />
          ) : taskControl.kind === "pause" ? (
            <Pause size={12} />
          ) : taskControl.kind === "resume" ? (
            <Play size={12} />
          ) : (
            <RotateCcw size={12} />
          )}
          {controlTaskBusy
            ? `${episodeActionLabel(taskControl.kind)}…`
            : episodeActionLabel(taskControl.kind)}
        </button>
      )}
      {onSwitchProvider && taskControl?.canSwitchProvider && (
        <button
          className="button compact"
          type="button"
          disabled={anotherActionBusy || controlTaskBusy}
          onClick={() => onSwitchProvider(taskControl.task)}
        >
          Switch provider…
        </button>
      )}
      {controls.continuationVisible && onContinue && (
        <form
          className="campaign-reauthorize"
          onSubmit={(event) => {
            event.preventDefault();
            void submitContinuation();
          }}
        >
          {controls.resumeTurns === null && (
            <input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              aria-label="Turns to add"
              value={additionalTurns}
              disabled={continuationBlocked}
              onChange={(event) => onAdditionalTurnsChange(event.target.value)}
            />
          )}
          <button
            className="button primary compact experiment-continue-button"
            type="submit"
            disabled={turns === null || continuationBlocked}
          >
            {continueBusy && <LoaderCircle className="spin" size={12} />}
            {continueBusy
              ? "Adding turns…"
              : controls.resumeTurns !== null
                ? `Resume · ${controls.resumeTurns} ${controls.resumeTurns === 1 ? "turn" : "turns"}`
                : parsedAdditionalTurns !== null
                  ? `Add ${parsedAdditionalTurns} turns`
                  : "Add turns"}
          </button>
        </form>
      )}
      {showStop && (
        <button
          type="button"
          className={
            experimentControl
              ? "button compact experiment-stop-loop"
              : "button secondary compact campaign-stop"
          }
          disabled={anotherActionBusy || controls.stopPending}
          onClick={() => void report(() => onStop(episodeId))}
        >
          {stopBusy ? <LoaderCircle className="spin" size={12} /> : <Square size={12} />}
          {stopBusy ? "Stopping…" : "Stop"}
        </button>
      )}
    </>
  );
}

function episodeActionLabel(action: EpisodeTaskAction): string {
  if (action === "pause") return "Pause";
  if (action === "resume") return "Resume";
  return "Retry";
}
