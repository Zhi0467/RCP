import { ArrowLeftRight, LoaderCircle, Play, Plus } from "lucide-react";
import type {
  AgentTask,
  Episode,
  ExperimentControlState,
  RunAction,
  RunPresentation,
} from "../core/types";
import { TaskActionButton, taskActionItem } from "../agents/taskActions";
import { OverflowMenu, type OverflowMenuItem } from "../ui/OverflowMenu";
import { episodeCanSwitchProvider } from "./campaigns";
import { authorizedInvocationCount, pausedResumeTurns } from "./runProjection";
import type { EpisodeTaskAction } from "./episodeActions";

const TURN_ACTIONS: readonly RunAction["kind"][] = ["pause", "resume", "retry"];

interface Props {
  /** The run as the server published it; its actions are the only offers. */
  presentation: RunPresentation | null;
  episode: Episode | null;
  /** The Experiment's control, for what only it knows: an unsettled Stop and a provider switch. */
  experimentControl?: ExperimentControlState | null;
  /** The turn a turn control acts on, where the host holds it; unset, it is found on the episode. */
  controlTask?: AgentTask | null;
  additionalTurns: string;
  onAdditionalTurnsChange: (value: string) => void;
  disabled?: boolean;
  continuationDisabled?: boolean;
  /** The episode action in flight, as `stop:<episode id>` or `continue:<episode id>`. */
  busyAction: string | null;
  taskActionId: string | null;
  /** A surface offers only the controls it handles; an absent handler hides its offer. */
  onOperateTask?: (task: AgentTask, action: EpisodeTaskAction) => Promise<void>;
  onSwitchProvider?: (task: AgentTask) => void;
  onContinue?: (episodeId: string, invocationCeiling: number) => Promise<void>;
  onStop?: (episodeId: string) => Promise<void>;
  onError?: (message: string | null) => void;
  /** `menu` draws the same offers as rows of one ⋯ menu. */
  variant?: "buttons" | "menu";
  /** The menu's accessible name. */
  menuLabel?: string;
  /** Rows the host adds above the run's own, such as Open agent. */
  menuItems?: readonly OverflowMenuItem[];
}

/**
 * A run's controls exactly as its presentation offers them: the current turn's
 * Stop, Resume or Retry, Add turns, and Stop. Every surface draws this one
 * component, as buttons or as a menu.
 */
export function EpisodeControls({
  presentation,
  episode,
  experimentControl,
  controlTask,
  additionalTurns,
  onAdditionalTurnsChange,
  disabled = false,
  continuationDisabled = false,
  busyAction,
  taskActionId,
  onOperateTask,
  onSwitchProvider,
  onContinue,
  onStop,
  onError,
  variant = "buttons",
  menuLabel = "Run actions",
  menuItems = [],
}: Props) {
  const actions = presentation?.actions ?? [];
  const turnAction = onOperateTask
    ? (actions.find((action) => TURN_ACTIONS.includes(action.kind)) ?? null)
    : null;
  const turnKind = turnAction?.kind as EpisodeTaskAction | undefined;
  const task =
    turnAction === null
      ? null
      : controlTask !== undefined
        ? controlTask
        : (episode?.tasks.find((member) => member.operation_id === turnAction.operation_id) ??
          null);
  const stopAction = onStop ? (actions.find((action) => action.kind === "stop") ?? null) : null;
  const continueAction =
    onContinue && !experimentControl?.node_closed
      ? (actions.find((action) => action.kind === "continue") ?? null)
      : null;
  const stopPending = Boolean(experimentControl?.stop_pending);
  const continuationReady =
    continueAction !== null &&
    (!experimentControl || (experimentControl.can_start && !experimentControl.stop_pending));
  const resumeTurns = continueAction ? pausedResumeTurns(episode) : null;
  const canSwitchProvider =
    Boolean(onSwitchProvider && task && turnKind) &&
    (experimentControl
      ? experimentControl.can_switch_provider
      : turnKind === "retry" && episode !== null && episodeCanSwitchProvider(episode, task!));

  const stopId = stopAction?.episode_id ?? episode?.episode_id ?? "";
  const continueId = continueAction?.episode_id ?? "";
  const parsedAdditionalTurns = authorizedInvocationCount(additionalTurns);
  const turns = resumeTurns ?? parsedAdditionalTurns;
  const stopBusy = busyAction === `stop:${stopId}`;
  const continueBusy = continueAction !== null && busyAction === `continue:${continueId}`;
  const taskBusy = task !== null && taskActionId === task.operation_id;
  const anotherActionBusy = disabled || busyAction !== null || taskActionId !== null;
  const continuationBlocked = anotherActionBusy || continuationDisabled || !continuationReady;
  const showStop = Boolean(onStop) && (stopAction !== null || stopBusy);
  const report = (run: () => Promise<void>) => {
    onError?.(null);
    return run().catch((error: unknown) =>
      onError?.(error instanceof Error ? error.message : String(error)),
    );
  };
  const operate = () => {
    if (task && turnKind && onOperateTask && !anotherActionBusy)
      void report(() => onOperateTask(task, turnKind));
  };
  const stop = () => {
    if (onStop) void report(() => onStop(stopId));
  };
  const submitContinuation = async () => {
    if (turns === null || continuationBlocked || !onContinue) return;
    await report(async () => {
      await onContinue(continueId, turns);
      onAdditionalTurnsChange("");
    });
  };
  const turnLabel = turnKind === "pause" && showStop ? "Stop turn" : undefined;
  const continueLabel = continueBusy
    ? resumeTurns !== null
      ? "Resuming…"
      : "Adding turns…"
    : resumeTurns !== null
      ? `Resume · ${resumeTurns} ${resumeTurns === 1 ? "turn" : "turns"}`
      : parsedAdditionalTurns !== null
        ? `Add ${parsedAdditionalTurns} turns`
        : "Add turns";
  const turnsInput = continueAction && resumeTurns === null && (
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
  );

  if (variant === "menu") {
    const items: OverflowMenuItem[] = [...menuItems];
    if (task && turnKind)
      items.push(
        taskActionItem(turnKind, {
          ...(turnLabel ? { label: turnLabel } : {}),
          disabled: anotherActionBusy,
          onSelect: operate,
        }),
      );
    if (canSwitchProvider && task)
      items.push({
        label: "Switch provider…",
        icon: ArrowLeftRight,
        disabled: anotherActionBusy,
        onSelect: () => onSwitchProvider?.(task),
      });
    if (continueAction)
      items.push({
        label: continueLabel,
        icon: resumeTurns !== null ? Play : Plus,
        disabled: turns === null || continuationBlocked,
        onSelect: () => void submitContinuation(),
      });
    if (showStop)
      items.push(
        taskActionItem("stop", { disabled: anotherActionBusy || stopPending, onSelect: stop }),
      );
    if (items.length === 0) return null;
    return (
      <OverflowMenu label={menuLabel} items={items} portal>
        {turnsInput && <label className="campaign-reauthorize">{turnsInput}</label>}
      </OverflowMenu>
    );
  }

  return (
    <>
      {task && turnKind && (
        <TaskActionButton
          action={turnKind}
          label={turnLabel}
          className={
            experimentControl
              ? "button primary compact experiment-recovery-button"
              : `button compact ${turnKind === "resume" ? "primary" : "secondary"}`
          }
          busy={taskBusy}
          disabled={anotherActionBusy}
          onClick={operate}
        />
      )}
      {canSwitchProvider && task && (
        <button
          className="button compact"
          type="button"
          disabled={anotherActionBusy || taskBusy}
          onClick={() => onSwitchProvider?.(task)}
        >
          Switch provider…
        </button>
      )}
      {continueAction && (
        <form
          className="campaign-reauthorize"
          onSubmit={(event) => {
            event.preventDefault();
            void submitContinuation();
          }}
        >
          {turnsInput}
          <button
            className="button primary compact experiment-continue-button"
            type="submit"
            disabled={turns === null || continuationBlocked}
            aria-busy={continueBusy || undefined}
          >
            {continueBusy && <LoaderCircle className="spin" size={12} />}
            {continueLabel}
          </button>
        </form>
      )}
      {showStop && (
        <TaskActionButton
          action="stop"
          className={
            experimentControl
              ? "button compact experiment-stop-loop"
              : "button secondary compact campaign-stop"
          }
          busy={stopBusy}
          disabled={anotherActionBusy || stopPending}
          onClick={stop}
        />
      )}
    </>
  );
}
