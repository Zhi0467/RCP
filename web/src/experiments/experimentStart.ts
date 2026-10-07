import { sameGraphTarget } from "../core/graphTarget.ts";
import type {
  ExperimentControlState,
  ExperimentLoopIndexEntry,
  GraphTargetRef,
  LoopOverlap,
} from "../core/types";

/** A new branch shares graph prerequisites, but not the source target's live loop. */
export function canStartExperiment(
  control: ExperimentControlState | null | undefined,
  target: GraphTargetRef,
  graphIsolation = false,
): boolean {
  if (!control) return false;
  return target.kind === "main" && graphIsolation
    ? control.isolated_start_reasons.length === 0
    : control.can_start;
}

/** Admission reasons come from the backend for the chosen start target. */
export function experimentStartReasons(
  control: ExperimentControlState | null | undefined,
  target: GraphTargetRef,
  graphIsolation = false,
): string[] {
  if (!control) return [];
  return target.kind === "main" && graphIsolation
    ? control.isolated_start_reasons
    : control.reasons;
}

/** A fresh isolated branch overlaps even with a loop on the displayed main target. */
export function experimentStartOverlap(
  entries: ExperimentLoopIndexEntry[],
  projectId: string,
  nodeId: string,
  target: GraphTargetRef,
  graphIsolation: boolean,
): LoopOverlap {
  return {
    rows: entries
      .filter(
        (entry) =>
          entry.project_id === projectId &&
          entry.node.id === nodeId &&
          entry.control.live &&
          ((target.kind === "main" && graphIsolation) ||
            !sameGraphTarget(entry.graph_target, target)),
      )
      .map(({ node, episode, graph_target }) => ({
        node_id: node.id,
        episode_id: episode.episode_id,
        graph_target,
        state: "live",
        started_by: {
          kind: episode.started_by.kind,
          id:
            episode.started_by.kind === "auto_research"
              ? episode.auto_research_parent_episode_id
              : episode.started_by.human?.user_id,
          display_name: episode.started_by.human?.display_name ?? null,
        },
        checkout: episode.checkout,
      })),
    omitted: 0,
  };
}
