// Pure logic for "Since you last looked": emptiness, the
// Research dots, and line labels. The hook and views own only React state.

import type {
  GraphTargetRef,
  ProjectDigest,
  ProjectDigestNeedsYou,
  ProjectDigestReport,
} from "../core/types";

export function projectDigestIsEmpty(digest: ProjectDigest | null): boolean {
  return (
    !digest ||
    (digest.needs_you.length === 0 && digest.changed.length === 0 && digest.reports.length === 0)
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
  episode: "Experiment",
  auto_research: "Auto-research",
};

const REPORT_LABELS: Record<ProjectDigestReport["kind"], string> = {
  consolidation_report: "Consolidation",
  episode_report: "Run",
};

export function needsYouKindLabel(item: ProjectDigestNeedsYou): string {
  return NEEDS_YOU_LABELS[item.kind] ?? item.kind;
}

export function reportKindLabel(item: ProjectDigestReport): string {
  return REPORT_LABELS[item.kind] ?? item.kind;
}

/** The header line: what needs you, how many edits, how many reports. */
export function digestSummary(digest: ProjectDigest): string {
  const edits = digest.changed.reduce((total, change) => total + change.edits, 0);
  const needs = digest.needs_you.length;
  const reports = digest.reports.length;
  return [
    needs > 0 && `${needs} need${needs === 1 ? "s" : ""} you`,
    edits > 0 && editCountLabel(edits),
    reports > 0 && `${reports} report${reports === 1 ? "" : "s"}`,
  ]
    .filter(Boolean)
    .join(" · ");
}

export function editCountLabel(edits: number): string {
  return `${edits} edit${edits === 1 ? "" : "s"}`;
}
