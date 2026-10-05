import type { BrowserTransitionProjection } from "./graph/projectSession";
import { toHumanSyncRequest, type HumanDraft, type HumanSyncRequest } from "./graph/humanDraft";
import {
  transitionHeadsEqual,
  transitionPreviewRouting,
  type StagedTransitionEdit,
  type TransitionPreviewRouting,
} from "./graph/projectTransition";
import { isControlNode } from "./graph/researchType";
import type {
  ExperimentControlState,
  GraphAttentionProjection,
  GraphHeadRef,
  GraphNode,
  GraphState,
  ProjectSnapshot,
  ProjectTransitionResponse,
  TransitionPreviewResponse,
  TransitionTriggerManifest,
} from "./core/types";

export function experimentStartNeedsSync(projection: BrowserTransitionProjection | null): boolean {
  return projection?.base_head != null;
}

export function transitionProjectionForRoute(
  projection: BrowserTransitionProjection | null,
  route: TransitionPreviewRouting["route"],
): BrowserTransitionProjection | null {
  return route === "backend_preview" ? projection : null;
}

export function humanDraftTransitionRouting(
  draft: HumanDraft,
  graph: GraphState,
  manifest: TransitionTriggerManifest | null,
  rulesetTag: string | null,
): TransitionPreviewRouting {
  const request = toHumanSyncRequest(draft, graph);
  const edits: StagedTransitionEdit[] = request.nodes.map((item) => ({
    operation: "update_nodes",
    node_types: graph.nodes[item.node_id] ? [graph.nodes[item.node_id].type] : undefined,
    node_fields: [
      ...Object.keys(item.changes),
      ...(item.standing ? ["standing"] : []),
      ...(item.cancel_attempt_ids?.length ? ["attempts"] : []),
    ],
    relations: [],
  }));
  if (request.custom_nodes.length > 0) {
    edits.push({
      operation: "create_nodes",
      node_types: request.custom_nodes.map((node) => node.type),
      node_fields: [],
      relations: [],
    });
  }
  if (request.ontology) {
    edits.push({ operation: "set_ontology", node_types: [], node_fields: [], relations: [] });
  }
  const changesExperimentControl =
    request.nodes.some(
      (item) =>
        isControlNode(graph.nodes[item.node_id]?.type) &&
        Object.hasOwn(item.changes, "invocation_ceiling"),
    ) || request.custom_nodes.some((node) => isControlNode(node.type));
  // Removal expands to incident relation changes, and a Proposal decision may expand to any
  // Proposal operation. Experiment ceiling updates and new Experiments also change the coherent
  // control projection even though the current manifest does not list them. The browser does not
  // infer any outcomes; absent tags route all of these shapes to preview conservatively.
  if (
    request.removed_node_ids.length > 0 ||
    request.custom_nodes.length > 0 ||
    request.added_edges.length > 0 ||
    request.removed_edge_ids.length > 0 ||
    request.proposals.length > 0 ||
    changesExperimentControl
  ) {
    edits.push({});
  }
  for (const edit of edits) {
    const routing = transitionPreviewRouting(manifest, rulesetTag, edit);
    if (routing.route === "backend_preview") return routing;
  }
  return { route: "local_draft", reason: "no_manifest_trigger" };
}

export function proposalChoicesClearedNotice(proposalIds: string[]): string {
  return `Externally resolved proposal choices were cleared: ${proposalIds.join(", ")}.`;
}

export function humanSyncSuccessNotice(
  revision: number,
  submittedProposals: HumanSyncRequest["proposals"],
  nextGraph: GraphState,
): string {
  const withdrawnProposalIds = submittedProposals
    .filter((judgment) => nextGraph.proposals[judgment.proposal_id]?.status === "withdrawn")
    .map((judgment) => judgment.proposal_id)
    .sort();
  return withdrawnProposalIds.length > 0
    ? `Synced revision ${revision}. Stale proposals were withdrawn and their proposed changes were not applied: ${withdrawnProposalIds.join(", ")}.`
    : `Synced revision ${revision}.`;
}

export function localDraftTransitionProjection(
  graph: GraphState,
  experimentControl: Record<string, ExperimentControlState>,
  attention: GraphAttentionProjection,
  primaryQuestion: GraphNode | null,
  counts: ProjectSnapshot["counts"],
  head: GraphHeadRef,
  rulesetTag: string | null,
): BrowserTransitionProjection {
  return {
    head,
    graph,
    attention,
    primary_question: primaryQuestion,
    counts,
    experiment_control: experimentControl,
    ruleset_tag: rulesetTag,
    transition_id: head.transition_id,
    canonical: false,
    base_head: head,
  };
}

export function previewTraceMismatch(
  response: TransitionPreviewResponse,
  projection: ProjectTransitionResponse,
): string | null {
  if (projection.canonical) return "Staged transition preview was marked canonical.";
  if (!projection.base_head) return "Staged transition preview omitted its canonical base head.";
  if (!transitionHeadsEqual(projection.base_head, response.transition.pre_head)) {
    return "Staged transition preview base head did not match its transition trace.";
  }
  if (projection.transition_id !== response.transition.transition_id) {
    return "Staged transition preview id did not match its transition trace.";
  }
  if (projection.ruleset_tag !== response.transition.ruleset_tag) {
    return "Staged transition preview ruleset did not match its transition trace.";
  }
  return null;
}
