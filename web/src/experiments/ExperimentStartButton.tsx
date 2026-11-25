import { FlaskConical } from "lucide-react";
import type { ExperimentControlState } from "../core/types";

/**
 * Start for an Experiment, the same on its node and on Runs. It waits while a
 * Stop is still settling, and points at the listed reasons that hold it back.
 */
export function ExperimentStartButton({
  control,
  busy,
  disabled,
  reasonsId,
  onStart,
}: {
  control: Pick<ExperimentControlState, "episode_id" | "stop_pending">;
  busy: boolean;
  /** What the host alone knows: its admission answer, its picker, its draft. */
  disabled: boolean;
  /** The element listing why Start is refused, when one is shown. */
  reasonsId?: string;
  onStart: () => void;
}) {
  return (
    <button
      type="button"
      className="button primary compact experiment-run-button"
      disabled={disabled || busy || control.stop_pending}
      aria-busy={busy || undefined}
      aria-describedby={reasonsId}
      onClick={onStart}
    >
      <FlaskConical size={14} aria-hidden="true" />{" "}
      {busy ? "Starting" : control.episode_id ? "Start new episode" : "Start episode"}
    </button>
  );
}
