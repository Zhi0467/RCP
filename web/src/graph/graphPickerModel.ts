import { sameGraphTarget } from "../core/graphTarget.ts";
import type { AppView, GraphRef, GraphTargetRef } from "../core/types";

export function graphRefTarget(ref: GraphRef): GraphTargetRef {
  return ref.kind === "main" ? { kind: "main" } : { kind: "branch", branch_id: ref.branch_id };
}

/** An archived branch is silenced: it never shows the attention dot. */
export function refNeedsYou(ref: GraphRef): boolean {
  return ref.kind === "branch" && ref.needs_you && !ref.archived;
}

/** Main first, then running branches; newest first within each group. */
export function graphPickerOptions(
  refs: readonly GraphRef[],
  activeRef: GraphTargetRef,
): GraphRef[] {
  return refs
    .filter((ref) => !ref.archived || sameGraphTarget(graphRefTarget(ref), activeRef))
    .sort((a, b) => {
      if (a.kind === "main") return b.kind === "main" ? 0 : -1;
      if (b.kind === "main") return 1;
      return (
        Number(b.running) - Number(a.running) || Date.parse(b.created_at) - Date.parse(a.created_at)
      );
    });
}

export interface GraphPickerSelection {
  view: AppView;
  nodeId: string | null;
  chatId: string | null;
  episodeId: string | null;
}

export interface GraphPickerDestination {
  nodeIds: ReadonlySet<string>;
  chatIds: ReadonlySet<string>;
  episodeIds: ReadonlySet<string>;
  /** Supplied by the view owner; the picker does not invent main-only rules. */
  views: ReadonlySet<AppView>;
}

/** Call with the destination's loaded membership, never the old ref's snapshot. */
export function selectionAfterGraphSwitch(
  selection: GraphPickerSelection,
  destination: GraphPickerDestination,
): GraphPickerSelection {
  return {
    view: destination.views.has(selection.view) ? selection.view : "overview",
    nodeId:
      selection.nodeId !== null && destination.nodeIds.has(selection.nodeId)
        ? selection.nodeId
        : null,
    chatId:
      selection.chatId !== null && destination.chatIds.has(selection.chatId)
        ? selection.chatId
        : null,
    episodeId:
      selection.episodeId !== null && destination.episodeIds.has(selection.episodeId)
        ? selection.episodeId
        : null,
  };
}
