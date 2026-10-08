import { GitBranch } from "lucide-react";
import type { GraphTargetRef } from "../core/types";

export function ExperimentBranchBadge({
  target,
  autoResearchEpisodeId,
}: {
  target: GraphTargetRef;
  autoResearchEpisodeId?: string | null;
}) {
  const isAutoResearch = target.kind === "branch" && target.branch_id === autoResearchEpisodeId;
  const label =
    target.kind === "main"
      ? "Main"
      : `${isAutoResearch ? "Auto-research · " : ""}${target.branch_id.slice(0, 8)}`;
  return (
    <span
      className="status-pill experiment-branch-badge"
      title={target.kind === "main" ? label : target.branch_id}
      data-graph-target-kind={target.kind}
    >
      <GitBranch size={12} aria-hidden="true" />
      <span>{label}</span>
    </span>
  );
}
