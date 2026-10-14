import type {
  GraphEditOptions,
  GraphNode,
  GraphState,
  NewEdge,
  NewNode,
  OntologyFieldDefinition,
  OntologyState,
  BaseNodeType,
} from "../core/types";
import type { HumanDraft } from "./humanDraft";
import {
  activeCustomTypes,
  activeFieldsForNode,
  humanNodeId,
  normalizeSlug,
} from "./ontologyEditing.ts";
import { carriesOrigin, FLOW_ORDER, primaryField } from "./researchType.ts";

/**
 * Everything the DAG canvas needs to edit the human draft. App owns the draft;
 * the canvas only calls these. Every callback is one draft update, so one undo step.
 */
export interface GraphEditingProps {
  projectId: string;
  mutationsDisabled?: boolean;
  /** Backend edit options; null while loading or after a failed load. */
  editOptions: GraphEditOptions | null;
  /** Present only after the options failed to load: fetch them again. */
  onRetryEditOptions?: () => void;
  /** Canonical graph without the draft: removal guards and edge identity read it. */
  canonicalGraph: GraphState;
  /** The member's current draft, or null when nothing is staged. */
  humanDraft: HumanDraft | null;
  /** Experiments whose loop is active; they cannot be removed or merged away. */
  activeExperimentIds: string[];
  /** Stage a new node together with the links drawn with it. */
  onStageNode: (node: NewNode, edges: NewEdge[]) => void;
  /** Replace a staged, not yet synced node (rename, origin, fields). */
  onUpdateStagedNode: (node: NewNode) => void;
  onStageEdge: (edge: NewEdge) => void;
  /** Change a link's relation: remove `edgeId` and add `edge` together. */
  onReplaceEdge: (edgeId: string, edge: NewEdge) => void;
  /** Unlink. A draft-added link is unstaged; a canonical one is staged for removal. */
  onRemoveEdge: (edgeId: string) => void;
  /** Delete. Staged nodes are unstaged; canonical ones are staged for removal. */
  onRemoveNodes: (nodeIds: string[]) => void;
  /** Merge: copy the duplicate's links onto the canonical node and remove the duplicate. */
  onMergeNodes: (duplicateId: string, canonicalId: string) => void;
  /** Rename a canonical node (a title edit) or a staged one. */
  onRenameNode: (nodeId: string, title: string) => void;
  onUndo: () => void;
  onRedo: () => void;
  canUndo: boolean;
  canRedo: boolean;
}

/** One relation a link between two nodes may carry, oriented as it would be stored. */
export interface RelationChoice {
  relation: string;
  source: string;
  target: string;
  /** Same-type meta relations (supersedes, duplicate_of) sort after ontology ones. */
  meta: boolean;
  /** Evidence assessment (relevance and weight) must be chosen before staging. */
  needsAssessment: boolean;
  acceptsExpectation: boolean;
}

/** A type a new node may take, with the relation it gets when drawn from another node. */
export interface NewNodeTypeChoice {
  /** Built-in base type name or active custom type name. */
  name: string;
  base_type: BaseNodeType;
  relation: string | null;
  /** True when the link runs from the existing node to the new one. */
  outgoing: boolean;
}

/** Why a node cannot be removed (or merged away) right now, or null when it can. */
export type RemovalRefusal = "accepted" | "active_experiment" | "staged_edit";

export function nodeRemovalRefusal(
  canonicalGraph: GraphState,
  draft: HumanDraft | null,
  nodeId: string,
  activeExperimentIds: string[],
): RemovalRefusal | null {
  if (draft?.custom_nodes[nodeId]) return null;
  const node = canonicalGraph.nodes[nodeId];
  if (!node) return null;
  // Mirrors stageNodeRemoval as node detail calls it: only the active Experiment itself.
  if (node.standing === "accepted") return "accepted";
  if (activeExperimentIds.includes(nodeId)) return "active_experiment";
  if (draft?.nodes[nodeId]) return "staged_edit";
  return null;
}

/** Valid relations between two nodes in either direction, ontology ones first, existing links excluded. */
export function relationChoices(
  options: GraphEditOptions,
  ontology: OntologyState,
  nodes: Record<string, GraphNode>,
  edges: NewEdge[],
  a: string,
  b: string,
): RelationChoice[] {
  if (a === b || !nodes[a] || !nodes[b]) return [];
  const definitions = [
    ...options.relations.map((relation) => ({ ...relation, custom: false })),
    ...ontology.relations
      .filter((relation) => !relation.deprecated)
      .map((relation) => ({
        ...relation,
        custom: true,
        same_type: false,
        assessment_required_for: [],
        accepts_expectation: false,
      })),
  ];
  const choices: RelationChoice[] = [];
  for (const [source, target] of [
    [a, b],
    [b, a],
  ]) {
    const start = nodes[source];
    const end = nodes[target];
    for (const relation of definitions) {
      // Custom relations name semantic types (the backend's edge_matches_relation); built-ins name base types.
      const matches = (types: string[], node: GraphNode) =>
        types.includes(relation.custom ? (node.extension_type ?? node.type) : node.type);
      if (!matches(relation.source_types, start) || !matches(relation.target_types, end)) continue;
      // Built-in same-type relations compare base types only, as backend validation does.
      if (relation.same_type && start.type !== end.type) continue;
      if (
        edges.some(
          (edge) =>
            edge.source === source && edge.target === target && edge.relation === relation.name,
        )
      )
        continue;
      choices.push({
        relation: relation.name,
        source,
        target,
        meta: relation.same_type,
        needsAssessment: relation.assessment_required_for.some(
          (pair) => pair.source_type === start.type && pair.target_type === end.type,
        ),
        acceptsExpectation: relation.accepts_expectation,
      });
    }
  }
  return choices.sort((left, right) => Number(left.meta) - Number(right.meta));
}

/** Types a new node may take: all active types, or those linkable from `from`. */
export function newNodeTypeChoices(
  options: GraphEditOptions,
  ontology: OntologyState,
  from?: GraphNode,
): NewNodeTypeChoice[] {
  const types = [
    ...FLOW_ORDER.map((name) => ({ name, base_type: name })),
    ...activeCustomTypes(ontology),
  ];
  return types.flatMap<NewNodeTypeChoice>(({ name, base_type }) => {
    if (!from) return [{ name, base_type, relation: null, outgoing: true }];
    // A synthetic endpoint only distinguishes orientation; it is never persisted.
    const id = `${from.id}/canvas-new`;
    const target: GraphNode = {
      ...from,
      id,
      type: base_type,
      extension_type: name === base_type ? null : name,
    };
    const relation = relationChoices(
      options,
      ontology,
      { [from.id]: from, [id]: target },
      [],
      from.id,
      id,
    )[0];
    return relation
      ? [
          {
            name,
            base_type,
            relation: relation.relation,
            outgoing: relation.source === from.id,
          },
        ]
      : [];
  });
}

/** A committable node from a title alone: generated unique slug, main text = title, explicit origin where required. */
export function buildCanvasNode(
  choice: NewNodeTypeChoice,
  options: GraphEditOptions,
  title: string,
  takenIds: Set<string>,
  fields?: NewNode["extension_fields"],
): NewNode {
  const slug = normalizeSlug(title).slice(0, 60).replace(/-+$/, "") || "node";
  let id = humanNodeId(choice, options.node_prefixes, slug);
  for (let suffix = 2; takenIds.has(id); suffix += 1) {
    id = humanNodeId(choice, options.node_prefixes, `${slug}-${suffix}`);
  }
  return {
    id,
    type: choice.base_type,
    title: title.trim(),
    [primaryField(choice.base_type)]: title.trim(),
    ...(carriesOrigin(choice.base_type) ? { origin: "unknown" as const } : {}),
    extension_type: choice.name === choice.base_type ? null : choice.name,
    extension_fields: fields ?? {},
  };
}

/** Required custom fields the staged card must collect for a custom type. */
export function requiredCanvasFields(
  ontology: OntologyState,
  choice: NewNodeTypeChoice,
): OntologyFieldDefinition[] {
  return activeFieldsForNode(
    ontology,
    choice.base_type,
    choice.name === choice.base_type ? null : choice.name,
  ).filter((field) => field.required);
}
