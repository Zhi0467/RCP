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

/**
 * Everything the DAG canvas needs to edit the human draft. App owns the draft;
 * the canvas only calls these. Every callback is one draft update, so one undo step.
 */
export interface GraphEditingProps {
  projectId: string;
  mutationsDisabled?: boolean;
  /** Backend edit options; null while loading or after a failed load. */
  editOptions: GraphEditOptions | null;
  /** Canonical graph without the draft: removal guards and edge identity read it. */
  canonicalGraph: GraphState;
  /** The member's current draft, or null when nothing is staged. */
  humanDraft: HumanDraft | null;
  /** Whether the project's Experiment loop is active (removal guard). */
  experimentActive: boolean;
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
  experimentActive: boolean,
): RemovalRefusal | null {
  if (draft?.custom_nodes[nodeId]) return null;
  const node = canonicalGraph.nodes[nodeId];
  if (!node) return null;
  // Mirrors stageNodeRemoval: the loop guard covers every node, as the drawer does.
  if (node.standing === "accepted") return "accepted";
  if (experimentActive) return "active_experiment";
  if (draft?.nodes[nodeId]) return "staged_edit";
  return null;
}

/* The helpers below are declared here as the contract; slice 2 implements them. */

/** Valid relations between two nodes in either direction, ontology ones first, existing links excluded. */
export declare function relationChoices(
  options: GraphEditOptions,
  ontology: OntologyState,
  nodes: Record<string, GraphNode>,
  edges: NewEdge[],
  a: string,
  b: string,
): RelationChoice[];

/** Types a new node may take: all active types, or those linkable from `from`. */
export declare function newNodeTypeChoices(
  options: GraphEditOptions,
  ontology: OntologyState,
  from?: GraphNode,
): NewNodeTypeChoice[];

/** A committable node from a title alone: generated unique slug, main text = title, explicit origin where required. */
export declare function buildCanvasNode(
  choice: NewNodeTypeChoice,
  options: GraphEditOptions,
  title: string,
  takenIds: Set<string>,
  fields?: NewNode["extension_fields"],
): NewNode;

/** Required custom fields the staged card must collect for a custom type. */
export declare function requiredCanvasFields(
  ontology: OntologyState,
  choice: NewNodeTypeChoice,
): OntologyFieldDefinition[];
