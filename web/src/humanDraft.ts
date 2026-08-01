import type { GraphNode, GraphState, OntologyState, Standing } from "./types";

export type DraftNodeValue = string | number | boolean | string[] | Record<string, string | number | boolean | string[]> | null;
export type ProposalDecision = "approved" | "rejected";
export type AmbiguityDecision = "resolved" | "dismissed";

export interface DraftNodeChange {
  base_updated_rev: number;
  changes: Record<string, DraftNodeValue>;
  standing?: Standing;
  standing_origin?: "edit" | "judgment";
}

export interface HumanDraft {
  version: 1;
  base_revision: number;
  nodes: Record<string, DraftNodeChange>;
  proposals: Record<string, { decision: ProposalDecision; reason?: string }>;
  ambiguities: Record<string, { status: AmbiguityDecision }>;
  ontology: OntologyState | null;
  custom_nodes: Record<string, GraphNode>;
}

export interface HumanSyncRequest {
  base_revision: number;
  nodes: Array<{
    node_id: string;
    base_updated_rev: number;
    changes: Record<string, DraftNodeValue>;
    standing?: Standing;
  }>;
  proposals: Array<{ proposal_id: string; decision: ProposalDecision; reason?: string }>;
  ambiguities: Array<{ ambiguity_id: string; status: AmbiguityDecision }>;
  ontology: OntologyState | null;
  custom_nodes: GraphNode[];
}

export function emptyHumanDraft(baseRevision: number): HumanDraft {
  return {
    version: 1,
    base_revision: baseRevision,
    nodes: {},
    proposals: {},
    ambiguities: {},
    ontology: null,
    custom_nodes: {},
  };
}

export function normalizeHumanDraft(draft: HumanDraft, graph: GraphState): HumanDraft {
  if (draft.base_revision !== graph.revision) return cloneDraft(draft);
  const nodes = Object.fromEntries(Object.entries(draft.nodes).flatMap(([nodeId, entry]) => {
    const node = graph.nodes[nodeId];
    if (!node) return [];
    const changes = Object.fromEntries(
      Object.entries(entry.changes).filter(([key, value]) => !sameValue(node[key], value)),
    );
    let standing = entry.standing === node.standing ? undefined : entry.standing;
    let standingOrigin = standing ? entry.standing_origin : undefined;
    if (Object.keys(changes).length === 0 && standingOrigin === "edit") {
      standing = undefined;
      standingOrigin = undefined;
    }
    if (Object.keys(changes).length === 0 && standing === undefined) return [];
    return [[nodeId, {
      base_updated_rev: entry.base_updated_rev,
      changes,
      ...(standing ? { standing } : {}),
      ...(standingOrigin ? { standing_origin: standingOrigin } : {}),
    } satisfies DraftNodeChange]];
  }));
  const ontology = sameValue(draft.ontology, graph.ontology) ? null : draft.ontology;
  return { ...cloneDraft(draft), nodes, ontology };
}

export function stageNodeEdit(
  draft: HumanDraft,
  graph: GraphState,
  nodeId: string,
  changes: Record<string, DraftNodeValue>,
): HumanDraft {
  const node = graph.nodes[nodeId];
  if (!node) return draft;
  const existing = draft.nodes[nodeId];
  const effectiveStanding = existing?.standing ?? node.standing;
  const next = cloneDraft(draft);
  next.nodes[nodeId] = {
    base_updated_rev: existing?.base_updated_rev ?? node.updated_rev,
    changes: { ...existing?.changes, ...changes },
    ...(existing?.standing ? { standing: existing.standing } : {}),
    ...(existing?.standing_origin ? { standing_origin: existing.standing_origin } : {}),
  };
  if (Object.keys(changes).length > 0 && effectiveStanding !== "asserted") {
    next.nodes[nodeId].standing = "asserted";
    next.nodes[nodeId].standing_origin = "edit";
  }
  return normalizeHumanDraft(next, graph);
}

export function stageNodeEditStart(
  draft: HumanDraft,
  graph: GraphState,
  nodeId: string,
): HumanDraft {
  const node = graph.nodes[nodeId];
  if (!node) return draft;
  const existing = draft.nodes[nodeId];
  const next = cloneDraft(draft);
  next.nodes[nodeId] = {
    base_updated_rev: existing?.base_updated_rev ?? node.updated_rev,
    changes: { ...existing?.changes },
    standing: "asserted",
    standing_origin: "edit",
  };
  return next;
}

export function stageNodeStanding(
  draft: HumanDraft,
  graph: GraphState,
  nodeId: string,
  standing: Standing,
): HumanDraft {
  const node = graph.nodes[nodeId];
  if (!node) return draft;
  const existing = draft.nodes[nodeId];
  const next = cloneDraft(draft);
  next.nodes[nodeId] = {
    base_updated_rev: existing?.base_updated_rev ?? node.updated_rev,
    changes: { ...existing?.changes },
    standing,
    standing_origin: "judgment",
  };
  return normalizeHumanDraft(next, graph);
}

export function stageProposalDecision(
  draft: HumanDraft,
  proposalId: string,
  decision: ProposalDecision | null,
): HumanDraft {
  const next = cloneDraft(draft);
  if (decision) next.proposals[proposalId] = { decision };
  else delete next.proposals[proposalId];
  return next;
}

export function stageAmbiguityDecision(
  draft: HumanDraft,
  ambiguityId: string,
  status: AmbiguityDecision | null,
): HumanDraft {
  const next = cloneDraft(draft);
  if (status) next.ambiguities[ambiguityId] = { status };
  else delete next.ambiguities[ambiguityId];
  return next;
}

export function stageOntology(
  draft: HumanDraft,
  graph: GraphState,
  ontology: OntologyState,
): HumanDraft {
  return normalizeHumanDraft({ ...cloneDraft(draft), ontology }, graph);
}

export function stageCustomNode(draft: HumanDraft, node: GraphNode): HumanDraft {
  const next = cloneDraft(draft);
  next.custom_nodes[node.id] = { ...node, extension_fields: { ...node.extension_fields } };
  return next;
}

export function unstageCustomNode(draft: HumanDraft, nodeId: string): HumanDraft {
  const next = cloneDraft(draft);
  delete next.custom_nodes[nodeId];
  return next;
}

export function applyHumanDraft(graph: GraphState, draft: HumanDraft | null): GraphState {
  if (!draft) return graph;
  const nodes = { ...graph.nodes };
  for (const [nodeId, entry] of Object.entries(draft.nodes)) {
    const node = nodes[nodeId];
    if (!node) continue;
    nodes[nodeId] = {
      ...node,
      ...entry.changes,
      ...(entry.standing ? { standing: entry.standing } : {}),
      draft_touched: true,
    };
  }
  for (const [nodeId, node] of Object.entries(draft.custom_nodes)) {
    nodes[nodeId] = { ...node, draft_touched: true };
  }
  return { ...graph, nodes, ontology: draft.ontology ?? graph.ontology };
}

export function humanDraftChangeCount(draft: HumanDraft | null): number {
  if (!draft) return 0;
  const nodeChanges = Object.values(draft.nodes).reduce(
    (count, entry) => count + Object.keys(entry.changes).length + (entry.standing ? 1 : 0),
    0,
  );
  return nodeChanges
    + Object.keys(draft.proposals).length
    + Object.keys(draft.ambiguities).length
    + (draft.ontology ? 1 : 0)
    + Object.keys(draft.custom_nodes).length;
}

export function toHumanSyncRequest(draft: HumanDraft): HumanSyncRequest {
  return {
    base_revision: draft.base_revision,
    nodes: Object.entries(draft.nodes).map(([nodeId, entry]) => ({
      node_id: nodeId,
      base_updated_rev: entry.base_updated_rev,
      changes: entry.changes,
      ...(entry.standing ? { standing: entry.standing } : {}),
    })),
    proposals: Object.entries(draft.proposals).map(([proposalId, entry]) => ({
      proposal_id: proposalId,
      ...entry,
    })),
    ambiguities: Object.entries(draft.ambiguities).map(([ambiguityId, entry]) => ({
      ambiguity_id: ambiguityId,
      ...entry,
    })),
    ontology: draft.ontology,
    custom_nodes: Object.values(draft.custom_nodes),
  };
}

export function humanDraftStorageKey(projectId: string): string {
  return `rcp:human-draft:${projectId}`;
}

export function serializeHumanDraft(draft: HumanDraft): string {
  return JSON.stringify(draft);
}

export function deserializeHumanDraft(value: string | null): HumanDraft | null {
  if (!value) return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!isRecord(parsed) || parsed.version !== 1 || !Number.isInteger(parsed.base_revision)) return null;
    if (!isRecord(parsed.nodes) || !isRecord(parsed.proposals) || !isRecord(parsed.ambiguities)) return null;
    return {
      ...(parsed as unknown as HumanDraft),
      ontology: isRecord(parsed.ontology) ? parsed.ontology as unknown as OntologyState : null,
      custom_nodes: isRecord(parsed.custom_nodes) ? parsed.custom_nodes as Record<string, GraphNode> : {},
    };
  } catch {
    return null;
  }
}

function cloneDraft(draft: HumanDraft): HumanDraft {
  return {
    ...draft,
    nodes: Object.fromEntries(Object.entries(draft.nodes).map(([id, entry]) => [id, {
      ...entry,
      changes: { ...entry.changes },
    }])),
    proposals: Object.fromEntries(Object.entries(draft.proposals).map(([id, entry]) => [id, { ...entry }])),
    ambiguities: Object.fromEntries(Object.entries(draft.ambiguities).map(([id, entry]) => [id, { ...entry }])),
    ontology: draft.ontology ? {
      types: draft.ontology.types.map((item) => ({ ...item })),
      fields: draft.ontology.fields.map((item) => ({ ...item })),
      relations: draft.ontology.relations.map((item) => ({
        ...item,
        source_types: [...item.source_types],
        target_types: [...item.target_types],
      })),
    } : null,
    custom_nodes: Object.fromEntries(Object.entries(draft.custom_nodes).map(([id, node]) => [id, {
      ...node,
      extension_fields: { ...node.extension_fields },
      source_refs: [...node.source_refs],
    }])),
  };
}

function sameValue(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}
