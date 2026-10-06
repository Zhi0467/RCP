import type { ExperimentControlState, GraphTargetRef } from "../core/types";

/** A new branch shares graph prerequisites, but not the source target's live loop. */
export function canStartExperiment(
  control: ExperimentControlState | null | undefined,
  target: GraphTargetRef,
  graphIsolation = false,
): boolean {
  if (!control) return false;
  return target.kind === "main" && graphIsolation
    ? !control.node_closed && control.graph_reasons.length === 0
    : control.can_start;
}

/** The server orders reasons as graph prerequisites, closed status, then runtime gates. */
export function experimentStartReasons(
  control: ExperimentControlState | null | undefined,
  target: GraphTargetRef,
  graphIsolation = false,
): string[] {
  if (!control) return [];
  return target.kind === "main" && graphIsolation
    ? [
        ...control.graph_reasons,
        ...control.reasons.slice(
          control.graph_reasons.length,
          control.graph_reasons.length + Number(control.node_closed),
        ),
      ]
    : control.reasons;
}
