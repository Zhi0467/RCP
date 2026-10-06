import { GitBranch } from "lucide-react";
import type { GraphTargetRef } from "../core/types";

export function ExperimentBranchBadge({ target }: { target: GraphTargetRef }) {
  if (target.kind === "main") return null;
  return (
    <span className="status-pill experiment-branch-badge" title={target.branch_id}>
      <GitBranch size={12} aria-hidden="true" />
      <span>{target.branch_id}</span>
    </span>
  );
}
