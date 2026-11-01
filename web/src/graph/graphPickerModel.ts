import { branchName } from "../core/branchNames.ts";
import { sameGraphTarget } from "../core/graphTarget.ts";
import type { AppView, GraphRef, GraphTargetRef } from "../core/types";

export function graphRefTarget(ref: GraphRef): GraphTargetRef {
  return ref.kind === "main" ? { kind: "main" } : { kind: "branch", branch_id: ref.branch_id };
}

/** An archived branch is silenced: it never shows the attention dot. */
export function refNeedsYou(ref: GraphRef): boolean {
  return ref.kind === "branch" && ref.needs_you && !ref.archived;
}

/** Parent before descendants; running then newest among siblings. */
export function graphPickerOptions(
  refs: readonly GraphRef[],
  activeRef: GraphTargetRef,
): GraphRef[] {
  const visible = refs
    .filter((ref) => !ref.archived || sameGraphTarget(graphRefTarget(ref), activeRef))
    .sort((a, b) => {
      if (a.kind === "main") return b.kind === "main" ? 0 : -1;
      if (b.kind === "main") return 1;
      return (
        Number(b.running) - Number(a.running) || Date.parse(b.created_at) - Date.parse(a.created_at)
      );
    });
  const ordered: GraphRef[] = [];
  const visited = new Set<GraphRef>();
  const append = (ref: GraphRef) => {
    if (visited.has(ref)) return;
    visited.add(ref);
    ordered.push(ref);
    for (const child of visible) {
      if (
        child.kind === "branch" &&
        child.parent_head?.target.kind === "branch" &&
        child.parent_head.target.branch_id === ref.branch_id
      )
        append(child);
    }
  };
  visible
    .filter((ref) => {
      const parent = ref.kind === "branch" ? ref.parent_head?.target : null;
      return (
        parent?.kind !== "branch" || !visible.some((item) => item.branch_id === parent.branch_id)
      );
    })
    .forEach(append);
  visible.forEach(append);
  return ordered;
}

export function graphRefParentLabel(ref: GraphRef, refs: readonly GraphRef[]): string | null {
  if (ref.kind !== "branch") return null;
  const head = ref.parent_head;
  return head ? `from ${branchName(refs, head.target)}@${head.revision}` : null;
}

/** Backend eligibility applies to both episode-owned branches and member forks. */
export function graphRefCanMerge(ref: GraphRef): boolean {
  return ref.kind === "branch" && !ref.archived && ref.merge_eligible;
}

export function graphRefDepth(ref: GraphRef, refs: readonly GraphRef[]): number {
  const seen = new Set<string>();
  let depth = 0;
  while (ref.kind === "branch" && ref.parent_head?.target.kind === "branch") {
    const id = ref.parent_head.target.branch_id;
    if (seen.has(id)) break;
    seen.add(id);
    const parent = refs.find((item) => item.branch_id === id);
    if (!parent) break;
    depth++;
    ref = parent;
  }
  return depth;
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
