// Pure logic for "Since you last looked": request fencing, emptiness, the
// Research dots, and line labels. The hook and views own only React state.

import type {
  GraphTargetRef,
  ProjectDigest,
  ProjectDigestNeedsYou,
  ProjectDigestRan,
} from "./types";

export function projectDigestIsEmpty(digest: ProjectDigest | null): boolean {
  return (
    !digest ||
    (digest.needs_you.length === 0 &&
      digest.changed.length === 0 &&
      digest.branches.length === 0 &&
      digest.ran.length === 0)
  );
}

const NO_NODES: ReadonlySet<string> = new Set();

/** The digest describes main, so dots appear only while main is the open target. */
export function digestChangedNodeIds(
  digest: ProjectDigest | null,
  target: GraphTargetRef,
): ReadonlySet<string> {
  if (!digest || target.kind !== "main" || digest.changed_node_ids.length === 0) return NO_NODES;
  return new Set(digest.changed_node_ids);
}

/**
 * Newest request wins. A response applies only for the project it was issued
 * for and only when no newer response has applied, so a late response for an
 * older request or another project is dropped in either arrival order.
 * `invalidate` drops everything in flight (used after Caught up, so a read
 * started before the mark moved cannot repaint the old lines).
 */
export interface DigestRequestFence {
  begin(projectId: string): number;
  accept(projectId: string, generation: number): boolean;
  invalidate(): void;
}

export function createDigestRequestFence(): DigestRequestFence {
  let projectId: string | null = null;
  let issued = 0;
  let applied = 0;
  return {
    begin(nextProjectId) {
      if (nextProjectId !== projectId) {
        projectId = nextProjectId;
        applied = issued;
      }
      issued += 1;
      return issued;
    },
    accept(responseProjectId, generation) {
      if (responseProjectId !== projectId || generation <= applied) return false;
      applied = generation;
      return true;
    },
    invalidate() {
      applied = issued;
    },
  };
}

/**
 * Caught up acknowledges exactly the digest on screen: it posts that digest's
 * cursor, then reads again, so anything recorded after the cursor stays new.
 */
export async function catchUpProjectDigest(
  projectId: string,
  onScreen: ProjectDigest,
  io: {
    mark: (projectId: string, seq: number) => Promise<unknown>;
    reload: () => Promise<void>;
  },
): Promise<void> {
  await io.mark(projectId, onScreen.cursor);
  await io.reload();
}

const NEEDS_YOU_LABELS: Record<ProjectDigestNeedsYou["kind"], string> = {
  proposal: "Proposal",
  decision: "Decision",
  question: "Question",
  episode: "Episode",
};

const RAN_LABELS: Record<ProjectDigestRan["kind"], string> = {
  episode_ended: "Episode ended",
  job_ended: "Job ended",
  task_failed: "Task failed",
  consolidation_report: "Consolidation report",
  consolidation_failed: "Consolidation failed",
  episode_report: "Episode report",
};

export function needsYouKindLabel(item: ProjectDigestNeedsYou): string {
  return NEEDS_YOU_LABELS[item.kind] ?? item.kind;
}

export function ranKindLabel(item: ProjectDigestRan): string {
  return RAN_LABELS[item.kind] ?? item.kind;
}

export function editCountLabel(edits: number): string {
  return `${edits} edit${edits === 1 ? "" : "s"}`;
}
