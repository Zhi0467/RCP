import { LoaderCircle, Pause, Play, RotateCcw, Square } from "lucide-react";
import { useState } from "react";
import type { AgentTask, Episode } from "../core/types";
import type { EpisodeProjection } from "./campaigns";
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
}: {
  episode: Episode;
  taskControl: EpisodeProjection["taskControl"];
  /** The episode action in flight, as `stop:<episode id>` or `continue:<episode id>`. */
  busyAction: string | null;
  taskActionId: string | null;
  onOperateTask: (task: AgentTask, action: EpisodeTaskAction) => Promise<void>;
  onSwitchProvider?: (task: AgentTask) => void;
  onContinue: (episodeId: string, invocationCeiling: number) => Promise<void>;
  onStop: (episodeId: string) => Promise<void>;
  onError: (message: string | null) => void;
}) {
  const [additionalTurns, setAdditionalTurns] = useState("");
  const parsedAdditionalTurns = Number(additionalTurns);
  const continuationIsValid =
    additionalTurns.trim().length > 0 &&
    Number.isSafeInteger(parsedAdditionalTurns) &&
    parsedAdditionalTurns >= 1;
  const stopBusy = busyAction === `stop:${episode.episode_id}`;
  const continueBusy = busyAction === `continue:${episode.episode_id}`;
  const controlTaskBusy = taskControl !== null && taskActionId === taskControl.task.operation_id;
  const anotherActionBusy = busyAction !== null || taskActionId !== null;
  const showStop = episode.can_stop || stopBusy;
  const report = (run: () => Promise<void>) => {
    onError(null);
    return run().catch((error: unknown) =>
      onError(error instanceof Error ? error.message : String(error)),
    );
  };

  const submitContinuation = async () => {
    if (!continuationIsValid || anotherActionBusy) return;
    await report(async () => {
      await onContinue(episode.episode_id, parsedAdditionalTurns);
      setAdditionalTurns("");
    });
  };

  return (
    <>
      {taskControl && (
        <button
          className={`button compact ${taskControl.kind === "resume" ? "primary" : "secondary"}`}
          type="button"
          disabled={anotherActionBusy}
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
      {onSwitchProvider && taskControl?.kind === "retry" && taskControl.canSwitchProvider && (
        <button
          className="button compact"
          type="button"
          disabled={anotherActionBusy || controlTaskBusy}
          onClick={() => onSwitchProvider(taskControl.task)}
        >
          Switch provider…
        </button>
      )}
      {episode.can_continue && (
        <form
          className="campaign-reauthorize"
          onSubmit={(event) => {
            event.preventDefault();
            void submitContinuation();
          }}
        >
          <input
            type="number"
            min={1}
            step={1}
            inputMode="numeric"
            aria-label="Turns to add"
            value={additionalTurns}
            disabled={anotherActionBusy}
            onChange={(event) => setAdditionalTurns(event.target.value)}
          />
          <button
            className="button primary compact"
            type="submit"
            disabled={!continuationIsValid || anotherActionBusy}
          >
            {continueBusy && <LoaderCircle className="spin" size={12} />}
            {continueBusy
              ? "Adding turns…"
              : continuationIsValid
                ? `Add ${parsedAdditionalTurns} turns`
                : "Add turns"}
          </button>
        </form>
      )}
      {showStop && (
        <button
          className="button secondary compact campaign-stop"
          type="button"
          disabled={anotherActionBusy}
          onClick={() => void report(() => onStop(episode.episode_id))}
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
