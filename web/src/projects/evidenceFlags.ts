import type { EvidenceFlag, GraphNode } from "../core/types";
import { isOutcome, lifecycleField, projectType } from "../graph/researchType";

export type EvidenceFlagAction =
  "address" | "open_chat" | "open_evidence" | "open_source" | "dismiss";
export interface EvidenceFlagRow {
  flag: EvidenceFlag;
  node: GraphNode;
  actions: EvidenceFlagAction[];
}

/** Only current records participate in the Inbox count and graph marks. */
export function evidenceFlagRows(
  flags: readonly EvidenceFlag[],
  nodes: Record<string, GraphNode>,
): EvidenceFlagRow[] {
  return flags.flatMap((flag) => {
    const node = nodes[flag.evidence_id];
    if (
      flag.state === "dismissed" ||
      !node ||
      !isOutcome(node.type) ||
      node[lifecycleField(node.type)] === projectType().retiredValue
    )
      return [];
    const actions: EvidenceFlagAction[] = [
      flag.state === "addressing" ? "open_chat" : "address",
      "open_evidence",
    ];
    if (flag.source) actions.push("open_source");
    actions.push("dismiss");
    return [{ flag, node, actions }];
  });
}
