import type { BrowserTransitionProjection } from "./graph/projectSession";
import { applyHumanDraft, type HumanDraft } from "./graph/humanDraft";
import type { TransitionPreviewRouting } from "./graph/projectTransition";
import { isBlocker, isChooser } from "./graph/researchType";
import type {
  GraphAttentionProjection,
  GraphNode,
  GraphState,
  ProjectSnapshot,
} from "./core/types";

export function humanAttentionBlockers(
  blockerIds: readonly string[],
  presentedNodes: GraphState["nodes"],
): GraphNode[] {
  return blockerIds.map((nodeId) => {
    const node = presentedNodes[nodeId];
    if (!isBlocker(node?.type)) {
      throw new Error(`Attention member ${nodeId} is not a presented Blocker.`);
    }
    return node;
  });
}

export function decisionsAwaitingChoice(
  decisionIds: readonly string[],
  membershipNodes: GraphState["nodes"],
  presentedNodes: GraphState["nodes"],
): GraphNode[] {
  return decisionIds.map((nodeId) => {
    const membershipNode = membershipNodes[nodeId];
    const presented = presentedNodes[nodeId] ?? membershipNode;
    if (!isChooser(membershipNode?.type) || !isChooser(presented?.type)) {
      throw new Error(`Attention member ${nodeId} is not a presented Decision.`);
    }
    return { ...presented, status: membershipNode.status };
  });
}

const EMPTY_GRAPH_ATTENTION: GraphAttentionProjection = {
  pending_proposal_ids: [],
  decisions_awaiting_choice_ids: [],
  open_blocker_ids: [],
  proposal_actions: {},
  decision_prior_choices: {},
};

export function projectAttentionForPresentation(
  project: ProjectSnapshot | null,
  projection: BrowserTransitionProjection | null,
): GraphAttentionProjection {
  if (projection) {
    if (!projection.attention) {
      throw new Error("Transition projection omitted graph attention.");
    }
    return projection.attention;
  }
  if (project) {
    if (!project.attention) {
      throw new Error("Project snapshot omitted graph attention.");
    }
    return project.attention;
  }
  return EMPTY_GRAPH_ATTENTION;
}

export function attentionGraphForProjection(
  canonicalGraph: GraphState,
  projection: BrowserTransitionProjection | null,
  route: TransitionPreviewRouting["route"] = "backend_preview",
  draft: HumanDraft | null = null,
): GraphState {
  if (route === "local_draft") return applyHumanDraft(canonicalGraph, draft);
  return projection?.graph ?? canonicalGraph;
}
