import { sameGraphTarget } from "../core/graphTarget.ts";
import type { AppView, GraphRef, GraphTargetRef } from "../core/types";

export function graphRefTarget(ref: GraphRef): GraphTargetRef {
  return ref.kind === "main" ? { kind: "main" } : { kind: "branch", branch_id: ref.branch_id };
}

const GRAPH_REF_LABEL_MAX = 60;

/** A branch is named by its owner's title; a short id only when the backend has none. */
export function graphRefLabel(ref: GraphRef): string {
  if (ref.kind === "main") return "Main";
  const title = ref.title?.trim().split("\n")[0]?.trim();
  if (!title) return ref.branch_id.slice(0, 8);
  return title.length > GRAPH_REF_LABEL_MAX ? `${title.slice(0, GRAPH_REF_LABEL_MAX - 1)}…` : title;
}

/** The active branch when the loaded list does not have it yet (a branch newer than the list). */
export function unlistedActiveBranch(
  refs: readonly GraphRef[],
  activeRef: GraphTargetRef,
): string | null {
  return activeRef.kind === "branch" &&
    !refs.some((ref) => sameGraphTarget(graphRefTarget(ref), activeRef))
    ? activeRef.branch_id
    : null;
}

/** Main first; retain the API's branch order and always expose the active ref. */
export function graphPickerOptions(
  refs: readonly GraphRef[],
  activeRef: GraphTargetRef,
  showArchived = false,
): GraphRef[] {
  const visible = refs.filter(
    (ref) => !ref.archived || showArchived || sameGraphTarget(graphRefTarget(ref), activeRef),
  );
  return [
    ...visible.filter((ref) => ref.kind === "main"),
    ...visible.filter((ref) => ref.kind !== "main"),
  ];
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
