import { GitBranch } from "lucide-react";
import type { GraphTargetRef } from "../core/types";

export function ExperimentBranchBadge({ target }: { target: GraphTargetRef }) {
  const label = target.kind === "main" ? "Main" : target.branch_id;
  return (
    <span
      className="status-pill experiment-branch-badge"
      title={label}
      data-graph-target-kind={target.kind}
    >
      <GitBranch size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
