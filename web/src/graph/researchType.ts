/**
 * The research project type, as the Web client sees it.
 *
 * `RESEARCH` mirrors the backend answers in `rcp.core.research_type` (which
 * node types play which part), and the tables below hold the research
 * presentation: fields, labels, primary fields, status options, colors, and
 * lane order. Generic modules ask these predicates and tables instead of naming
 * research node types; only the research layer (`RESEARCH_LAYER` in
 * `web/tests/projectType.test.mjs`) names them. See
 * docs/decisions/2026-10-04-the-kernel-asks-node-type-questions.md.
 *
 * Custom ontology types are stored as their base type, so every answer here is
 * keyed by the base `node.type`.
 */
import type { NodeEditField } from "./nodeEditing";
import type { BaseNodeType, GraphNode, OntologyLayer } from "../core/types";

type NodeTypeName = string | null | undefined;

/** The answers one kind of project gives about its node types. */
export interface ProjectType {
  name: string;
  /** Base node types in their canonical order. */
  nodeTypes: readonly BaseNodeType[];
  /** The project's own (non-meta) relations. */
  relations: readonly string[];
  /** Reader-facing singular and plural names, as agent contracts spell them. */
  labels: Readonly<Record<BaseNodeType, string>>;
  pluralLabels: Readonly<Record<BaseNodeType, string>>;
  /** Existing nodes of these types change only through a Proposal (invariant 3b). */
  protectedBeliefTypes: ReadonlySet<string>;
  /** Beliefs that outcomes bear on. */
  beliefTypes: ReadonlySet<string>;
  /** Nodes that record an observation bearing on a belief. */
  outcomeTypes: ReadonlySet<string>;
  /** Relations through which an outcome bears on a belief; they carry an assessment. */
  beliefOutcomeRelations: ReadonlySet<string>;
  /** The node a bounded loop drives. */
  controlNodeTypes: ReadonlySet<string>;
  /** Nodes whose outcome is a choice among options. */
  chooserTypes: ReadonlySet<string>;
  /** Nodes that record an obstacle gating action. */
  blockerTypes: ReadonlySet<string>;
  /** Nodes that frame the project's open questions. */
  questionTypes: ReadonlySet<string>;
  /** Structural relations into an existing protected belief. */
  protectedRelations: ReadonlySet<string>;
  /** Relations from an action node to a blocker that gates it. */
  blockingRelations: ReadonlySet<string>;
  /** The field supersede and merge retire, where it is not `defaultLifecycleField`. */
  lifecycleFields: Readonly<Partial<Record<BaseNodeType, string>>>;
  defaultLifecycleField: string;
  retiredValue: string;
}

export const RESEARCH: ProjectType = {
  name: "research",
  nodeTypes: ["research_question", "hypothesis", "decision", "experiment", "evidence", "blocker"],
  relations: [
    "has_subquestion",
    "has_hypothesis",
    "has_decision",
    "tests",
    "governed_by",
    "produces",
    "informs",
    "addresses",
    "blocked_by",
    "supports",
    "weakens",
    "contradicts",
    "refutes",
    "inconclusive",
    "requires_decision",
  ],
  labels: {
    research_question: "ResearchQuestion",
    hypothesis: "Hypothesis",
    decision: "Decision",
    experiment: "Experiment",
    evidence: "Evidence",
    blocker: "Blocker",
  },
  pluralLabels: {
    research_question: "ResearchQuestions",
    hypothesis: "Hypotheses",
    decision: "Decisions",
    experiment: "Experiments",
    evidence: "Evidence",
    blocker: "Blockers",
  },
  protectedBeliefTypes: new Set(["research_question", "hypothesis"]),
  beliefTypes: new Set(["hypothesis"]),
  outcomeTypes: new Set(["evidence"]),
  beliefOutcomeRelations: new Set([
    "supports",
    "weakens",
    "refutes",
    "inconclusive",
    "contradicts",
  ]),
  controlNodeTypes: new Set(["experiment"]),
  chooserTypes: new Set(["decision"]),
  blockerTypes: new Set(["blocker"]),
  questionTypes: new Set(["research_question"]),
  protectedRelations: new Set(["has_subquestion", "has_hypothesis"]),
  blockingRelations: new Set(["blocked_by"]),
  lifecycleFields: { evidence: "validity" },
  defaultLifecycleField: "status",
  retiredValue: "superseded",
};

/** The project type of the open project. Every project is a research project today. */
export function projectType(): ProjectType {
  return RESEARCH;
}

const has = (roles: ReadonlySet<string>, name: NodeTypeName) => name != null && roles.has(name);

export const isProtectedBelief = (type: NodeTypeName) => has(RESEARCH.protectedBeliefTypes, type);
export const isBelief = (type: NodeTypeName) => has(RESEARCH.beliefTypes, type);
export const isOutcome = (type: NodeTypeName) => has(RESEARCH.outcomeTypes, type);
export const isControlNode = (type: NodeTypeName) => has(RESEARCH.controlNodeTypes, type);
export const isChooser = (type: NodeTypeName) => has(RESEARCH.chooserTypes, type);
export const isBlocker = (type: NodeTypeName) => has(RESEARCH.blockerTypes, type);
export const isQuestion = (type: NodeTypeName) => has(RESEARCH.questionTypes, type);
export const isBeliefOutcomeRelation = (relation: NodeTypeName) =>
  has(RESEARCH.beliefOutcomeRelations, relation);
export const isBlockingRelation = (relation: NodeTypeName) =>
  has(RESEARCH.blockingRelations, relation);
/** Outcomes record where their observation came from. */
export const carriesOrigin = isOutcome;

/** The field supersede and merge set to `RESEARCH.retiredValue` on `type`. */
export function lifecycleField(type: BaseNodeType): string {
  return RESEARCH.lifecycleFields[type] ?? RESEARCH.defaultLifecycleField;
}

/** `types` in canonical order. */
export function orderedTypes(types: Iterable<string>): BaseNodeType[] {
  const wanted = new Set(types);
  return RESEARCH.nodeTypes.filter((type) => wanted.has(type));
}

export function typeLabel(type: BaseNodeType, plural = false): string {
  return (plural ? RESEARCH.pluralLabels : RESEARCH.labels)[type];
}

// ---------------------------------------------------------------------------
// Presentation

export interface BaseTypePresentation {
  name: BaseNodeType;
  label: string;
  layer: OntologyLayer;
  primaryField: string;
  primaryLabel: string;
}

/** Base ontology types in canonical order, with the field that states each one. */
export const BASE_TYPE_PRESENTATION: BaseTypePresentation[] = [
  {
    name: "research_question",
    label: "Research question",
    layer: "epistemic",
    primaryField: "question",
    primaryLabel: "Question",
  },
  {
    name: "hypothesis",
    label: "Hypothesis",
    layer: "epistemic",
    primaryField: "statement",
    primaryLabel: "Statement",
  },
  {
    name: "decision",
    label: "Decision",
    layer: "action",
    primaryField: "question",
    primaryLabel: "Question",
  },
  {
    name: "experiment",
    label: "Experiment",
    layer: "action",
    primaryField: "objective",
    primaryLabel: "Objective",
  },
  {
    name: "evidence",
    label: "Evidence",
    layer: "epistemic",
    primaryField: "observation",
    primaryLabel: "Observation",
  },
  {
    name: "blocker",
    label: "Blocker",
    layer: "action",
    primaryField: "description",
    primaryLabel: "Description",
  },
];

const PRIMARY_FIELD = Object.fromEntries(
  BASE_TYPE_PRESENTATION.map((item) => [item.name, item.primaryField]),
) as Record<BaseNodeType, string>;

/** The field that states a node of `type`, shown in place of its title. */
export function primaryField(type: BaseNodeType): string {
  return PRIMARY_FIELD[type];
}

/** Reader-facing labels for node fields. */
export const FIELD_LABELS: Record<string, string> = {
  question: "Question",
  statement: "Claim",
  objective: "Objective",
  observation: "What was observed",
  description: "What is blocked",
  motivation: "Why this matters",
  rationale: "Reasoning",
  interpretation: "What it means",
  scope: "Scope",
  design: "How it will be tested",
  proxies: "What is measured in place of what",
  limitations: "What the measurement misses",
  current_summary: "Where things stand",
  next_action: "Next action",
  predictions: "What should happen if this is right",
  expected_outcomes: "Expected outcomes",
  interpretation_rules: "How results will be read",
  completion_criteria: "What counts as complete",
  options: "Options considered",
  selected_option: "Selected option",
  consequences: "What changes because of this",
  role: "Evidence role",
  legacy_strength: "Legacy strength (historical)",
  validity: "Validity",
  origin: "Origin",
  status: "Status",
  blocker_type: "Blocker type",
  resolution_condition: "What would unblock this",
  owner: "Owner",
  artifact_refs: "Artifacts",
};

/** The order in which a node's supporting fields are presented. */
export const CONTEXT_FIELD_ORDER = [
  "motivation",
  "rationale",
  "interpretation",
  "role",
  "legacy_strength",
  "scope",
  "design",
  "proxies",
  "limitations",
  "current_summary",
  "next_action",
  "predictions",
  "expected_outcomes",
  "interpretation_rules",
  "completion_criteria",
  "options",
  "selected_option",
  "consequences",
  "resolution_condition",
];

/** Fields a chooser presents in its own choice section rather than as context. */
export const CHOOSER_CHOICE_FIELDS: readonly string[] = ["options", "selected_option"];

const titleField: NodeEditField = { key: "title", label: "Title", kind: "text" };

/** Chooser statuses a human may set; a chooser in any other status keeps its status read-only. */
const CHOOSER_STATUS_OPTIONS = [
  { value: "open", label: "Open" },
  { value: "ready", label: "Ready" },
  { value: "revisit", label: "Revisit" },
];

/** The human-editable fields of each base type, in form order. */
export const EDIT_FIELDS_BY_TYPE: Record<BaseNodeType, NodeEditField[]> = {
  research_question: [
    titleField,
    { key: "question", label: "Question", kind: "multiline" },
    { key: "motivation", label: "Motivation", kind: "multiline" },
    { key: "scope", label: "Scope", kind: "multiline" },
  ],
  hypothesis: [
    titleField,
    { key: "statement", label: "Statement", kind: "multiline" },
    { key: "rationale", label: "Rationale", kind: "multiline" },
    { key: "predictions", label: "Predictions", kind: "list" },
    { key: "scope", label: "Scope", kind: "multiline" },
  ],
  decision: [
    titleField,
    { key: "question", label: "Question", kind: "multiline" },
    { key: "options", label: "Options", kind: "list" },
    { key: "status", label: "Status", kind: "select", options: CHOOSER_STATUS_OPTIONS },
    { key: "rationale", label: "Rationale", kind: "multiline", nullable: true },
    { key: "consequences", label: "Consequences", kind: "list" },
  ],
  experiment: [
    titleField,
    { key: "objective", label: "Objective", kind: "multiline" },
    { key: "design", label: "Design", kind: "multiline" },
    { key: "proxies", label: "Proxies", kind: "proxies" },
    { key: "limitations", label: "Limitations", kind: "list" },
    { key: "expected_outcomes", label: "Expected outcomes", kind: "list" },
    { key: "interpretation_rules", label: "Interpretation rules", kind: "list" },
    { key: "completion_criteria", label: "Completion criteria", kind: "list" },
    {
      key: "invocation_ceiling",
      label: "Invocation ceiling",
      kind: "number",
      min: 1,
      integer: true,
    },
    { key: "current_summary", label: "Current summary", kind: "multiline" },
    { key: "next_action", label: "Next action", kind: "multiline", nullable: true },
  ],
  evidence: [
    titleField,
    { key: "observation", label: "Observation", kind: "multiline" },
    { key: "interpretation", label: "Interpretation", kind: "multiline" },
  ],
  blocker: [
    titleField,
    {
      key: "status",
      label: "Status",
      kind: "select",
      options: [
        { value: "open", label: "Open" },
        { value: "resolved", label: "Resolved" },
        { value: "superseded", label: "Superseded" },
      ],
    },
    { key: "description", label: "Description", kind: "multiline" },
    { key: "resolution_condition", label: "Resolution condition", kind: "multiline" },
    { key: "recommended_action", label: "Recommended action", kind: "multiline", nullable: true },
  ],
};

/** The base fields a human may edit on `node`. */
export function editFieldsFor(node: Pick<GraphNode, "type" | "status">): NodeEditField[] {
  const fields = EDIT_FIELDS_BY_TYPE[node.type];
  if (
    isChooser(node.type) &&
    !CHOOSER_STATUS_OPTIONS.some((option) => option.value === node.status)
  ) {
    return fields.filter((field) => field.key !== "status");
  }
  return fields;
}

/**
 * Graph-view lens per type. Colors feed CSS custom properties, so naming the
 * palette tokens rather than their light values keeps the graph legible in
 * both themes.
 */
export const TYPE_LENS: Record<BaseNodeType, { label: string; color: string }> = {
  research_question: { label: "Questions", color: "var(--slate)" },
  hypothesis: { label: "Hypotheses", color: "var(--plum)" },
  decision: { label: "Decisions", color: "var(--mustard)" },
  experiment: { label: "Experiments", color: "var(--teal)" },
  evidence: { label: "Evidence", color: "var(--moss)" },
  blocker: { label: "Blockers", color: "var(--coral)" },
};

/** Left-to-right stage of each type in the semantic DAG layout. */
export const STAGE_BY_TYPE = {
  research_question: 0,
  hypothesis: 1,
  decision: 1,
  experiment: 2,
  blocker: 2,
  evidence: 3,
} as const satisfies Record<BaseNodeType, number>;

/** Lane order of the semantic DAG layout, which follows the research flow. */
export const FLOW_ORDER: readonly BaseNodeType[] = [
  "research_question",
  "hypothesis",
  "decision",
  "blocker",
  "experiment",
  "evidence",
];
