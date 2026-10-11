import { GitBranch } from "lucide-react";
import { branchName } from "./branchNames";
import type { GraphRef, GraphTargetRef } from "./types";

/** The one branch tag on Runs and Artifacts rows: `⑂ <name>`, `main` included. */
export function BranchTag({
  refs,
  target,
}: {
  refs: readonly GraphRef[] | null | undefined;
  target: GraphTargetRef;
}) {
  const name = branchName(refs, target);
  return (
    <span className="status-pill branch-tag" title={name} data-graph-target-kind={target.kind}>
      <GitBranch size={12} aria-hidden="true" />
      <span>{name}</span>
    </span>
  );
}
