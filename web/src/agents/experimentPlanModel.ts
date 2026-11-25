import { branchNameRefusal, branchNameRequest, branchNameValid } from "../graph/BranchNameField";
import { ApiError } from "../core/api";
import { errorMessage } from "../core/errors";
import { api } from "../core/api";
import { MAIN_GRAPH, graphTargetUrl } from "../core/graphTarget";
import type {
  AgentQuestion,
  AgentRunConfig,
  ApprovalCard,
  ApprovalCardAction,
  ApprovalCardDecision,
  ApprovalCardResult,
  ApprovalCardRevision,
  GraphNode,
  GraphState,
  GraphTargetRef,
} from "../core/types";
import { resolveApprovalCard } from "./agentThreadsApi";

export type AutoResearchAction = Extract<ApprovalCardAction, { kind: "auto_research" }>;
export type PlanAction = ExperimentStartAction | AutoResearchAction;

export type ExperimentStartAction = Extract<ApprovalCardAction, { kind: "experiment_start" }>;

/** The human's Start inputs, seeded from the card and edited in place. */
export interface PlanInputs {
  goal: string;
  invocationCeiling: number;
  config: AgentRunConfig | null;
}

export function planCardAction(card: ApprovalCard | null | undefined): PlanAction | null {
  return card?.action.kind === "experiment_start" || card?.action.kind === "auto_research"
    ? card.action
    : null;
}

/** Plan cards render in their turn's line; every other card keeps its usual place. */
export function isPlanCardQuestion(question: AgentQuestion): boolean {
  return planCardAction(question.card) !== null;
}

export function planGraphTarget(action: PlanAction): GraphTargetRef {
  return action.kind === "experiment_start" && action.branch_id
    ? { kind: "branch", branch_id: action.branch_id }
    : MAIN_GRAPH;
}

/** The highest turn limit a Start may ask for: the node's own limit. */
export function planTurnCap(action: ExperimentStartAction, node: GraphNode | null): number {
  return node?.invocation_ceiling ?? action.invocation_ceiling;
}

/** The inputs a fresh card shows: the agent's goal, limit and pick. */
export function initialPlanInputs(
  action: ExperimentStartAction,
  fallback: AgentRunConfig | null,
): PlanInputs {
  return {
    goal: action.goal,
    invocationCeiling: action.invocation_ceiling,
    config: initialPlanConfig(action, fallback),
  };
}

/** Whether the inputs can start: a live node, a goal, and a whole turn limit within the cap. */
export function planInputsReady(inputs: PlanInputs, node: GraphNode | null, cap: number): boolean {
  const limit = inputs.invocationCeiling;
  return (
    node !== null &&
    Boolean(inputs.goal.trim()) &&
    Number.isSafeInteger(limit) &&
    limit >= 1 &&
    limit <= cap
  );
}

/** The member's unsaved Design and Done when, with the node revision they started from. */
export interface PlanNodeDraft {
  design?: string;
  doneWhen?: string;
  nodeRev?: number;
}

/** The node's Design as the editor seeds it. */
export function nodeDesign(node: GraphNode | null): string {
  return typeof node?.design === "string" ? node.design : "";
}

export function criteriaLines(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

/** The node fields a draft would change, or null when it changes none. */
export function planNodeChanges(
  draft: PlanNodeDraft,
  node: GraphNode | null,
): Record<string, string | string[]> | null {
  if (!node) return null;
  const changes: Record<string, string | string[]> = {};
  if (draft.design !== undefined && draft.design.trim() !== nodeDesign(node).trim())
    changes.design = draft.design.trim();
  if (draft.doneWhen !== undefined) {
    const criteria = criteriaLines(draft.doneWhen);
    if (criteria.join("\n") !== (node.completion_criteria ?? []).join("\n"))
      changes.completion_criteria = criteria;
  }
  return Object.keys(changes).length ? changes : null;
}

/** Whether the node moved past the revision a Design or Done when draft started from. */
export function planNodeDraftStale(draft: PlanNodeDraft, node: GraphNode | null): boolean {
  return (
    (draft.design !== undefined || draft.doneWhen !== undefined) &&
    node !== null &&
    draft.nodeRev !== undefined &&
    node.updated_rev !== draft.nodeRev
  );
}

export interface PlanNodeSave {
  projectId: string;
  target: GraphTargetRef;
  nodeId: string;
  baseUpdatedRev: number;
  changes: Record<string, string | string[]>;
}

export interface PlanGraphPorts {
  read: (projectId: string, target: GraphTargetRef) => Promise<GraphState>;
  sync: (projectId: string, target: GraphTargetRef, body: unknown) => Promise<GraphState>;
}

const graphPath = (projectId: string, target: GraphTargetRef, suffix = "") =>
  graphTargetUrl(`/api/projects/${encodeURIComponent(projectId)}${suffix}`, target);

export const PLAN_GRAPH: PlanGraphPorts = {
  read: async (projectId, target) =>
    (await api<{ graph: GraphState }>(graphPath(projectId, target))).graph,
  sync: async (projectId, target, body) =>
    (
      await api<{ graph: GraphState }>(graphPath(projectId, target, "/sync"), {
        method: "POST",
        body: JSON.stringify(body),
      })
    ).graph,
};

export type PlanNodeSaveOutcome = { kind: "saved"; nodeRev: number } | { kind: "conflict" };

/**
 * One node change through the Sync route, from the revision the draft started at.
 * A refusal while the node itself still stands at that revision was the graph
 * moving, and is sent once more from a new read; a moved node is a conflict.
 */
export async function savePlanNodeEdit(
  save: PlanNodeSave,
  ports: PlanGraphPorts = PLAN_GRAPH,
): Promise<PlanNodeSaveOutcome> {
  for (let attempt = 0; ; attempt += 1) {
    const before = await ports.read(save.projectId, save.target);
    if (before.nodes[save.nodeId]?.updated_rev !== save.baseUpdatedRev) return { kind: "conflict" };
    try {
      const graph = await ports.sync(save.projectId, save.target, {
        base_revision: before.revision,
        nodes: [
          {
            node_id: save.nodeId,
            base_updated_rev: save.baseUpdatedRev,
            changes: save.changes,
          },
        ],
      });
      return { kind: "saved", nodeRev: graph.nodes[save.nodeId].updated_rev };
    } catch (failure) {
      if (!(failure instanceof ApiError && failure.status === 409) || attempt > 0) throw failure;
    }
  }
}

export type PlanDecisionKind = ApprovalCardDecision["decision"];

/** A plan tap's request: the pinned digest plus the human's own inputs and the node revision shown. */
export function planStartDecision(
  card: ApprovalCard,
  inputs: PlanInputs,
  node: Pick<GraphNode, "updated_rev">,
  decision: PlanDecisionKind = "approve",
): ApprovalCardDecision {
  return {
    decision,
    pinned_sha256: card.pinned_sha256,
    start: {
      kind: "experiment_start",
      goal: inputs.goal.trim(),
      invocation_ceiling: inputs.invocationCeiling,
      provider: inputs.config?.provider ?? null,
      model: inputs.config?.model || null,
      reasoning: inputs.config?.reasoning || null,
      node_rev: node.updated_rev,
    },
  };
}

export type PlanResolve = (
  questionId: string,
  decision: ApprovalCardDecision,
) => Promise<ApprovalCardResult | ApprovalCardRevision>;

const PLAN_CHANGED = "The plan changed since you opened it. Read it again, then Start.";

export type PlanTapOutcome =
  /** Settled, or taken by another tap whose outcome arrives with the refresh. */
  | { kind: "settled"; result: ApprovalCardResult | null }
  /** Parked or saved: the card stays open under the hash this tab adopts. */
  | { kind: "kept"; revision: ApprovalCardRevision }
  /** Revised since the member read it; the card stays open and refreshes. */
  | { kind: "revised" }
  /** Refused before the card was claimed; it stays open with the reason. */
  | { kind: "pending"; reason: string; planChanged: boolean; branchRefusal?: string };

/** One tap on a plan card. A moved plan or any refusal keeps the card open. */
export async function tapPlanCard(
  questionId: string,
  decision: ApprovalCardDecision,
  resolve: PlanResolve = resolveApprovalCard,
): Promise<PlanTapOutcome> {
  try {
    const result = await resolve(questionId, decision);
    return "pinned_sha256" in result
      ? { kind: "kept", revision: result }
      : { kind: "settled", result };
  } catch (failure) {
    if (failure instanceof ApiError && failure.code === "card_taken")
      return { kind: "settled", result: null };
    if (failure instanceof ApiError && failure.code === "plan_revised") return { kind: "revised" };
    if (failure instanceof ApiError && failure.code === "plan_changed")
      return {
        kind: "pending",
        reason: typeof failure.detail?.message === "string" ? failure.detail.message : PLAN_CHANGED,
        planChanged: true,
      };
    const branchRefusal = branchNameRefusal(failure);
    return {
      kind: "pending",
      reason: errorMessage(failure),
      planChanged: false,
      ...(branchRefusal ? { branchRefusal } : {}),
    };
  }
}

export interface AutoResearchInputs {
  instruction: string;
  branch: string;
  codeWorktree: boolean;
  worktreeRepository: string | null;
  invocationCeiling: number;
  config: AgentRunConfig | null;
}

export function initialAutoResearchInputs(
  action: AutoResearchAction,
  fallback: AgentRunConfig | null,
): AutoResearchInputs {
  return {
    instruction: action.starting_instruction ?? "",
    branch: action.branch_name ?? "",
    codeWorktree: action.code_worktree ?? true,
    worktreeRepository: action.worktree_repository ?? null,
    invocationCeiling: action.invocation_ceiling,
    config: initialPlanConfig(action, fallback),
  };
}

export function autoResearchInputsReady(inputs: AutoResearchInputs): boolean {
  return (
    Boolean(inputs.instruction.trim()) &&
    Number.isSafeInteger(inputs.invocationCeiling) &&
    inputs.invocationCeiling >= 1 &&
    branchNameValid(inputs.branch)
  );
}

export function autoResearchStartDecision(
  card: ApprovalCard,
  inputs: AutoResearchInputs,
  decision: PlanDecisionKind = "approve",
): ApprovalCardDecision {
  return {
    decision,
    pinned_sha256: card.pinned_sha256,
    start: {
      kind: "auto_research",
      starting_instruction: inputs.instruction.trim(),
      invocation_ceiling: inputs.invocationCeiling,
      branch_name: branchNameRequest(inputs.branch),
      code_worktree: inputs.codeWorktree,
      worktree_repository: inputs.codeWorktree ? inputs.worktreeRepository : null,
      provider: inputs.config?.provider ?? null,
      model: inputs.config?.model || null,
      reasoning: inputs.config?.reasoning || null,
    },
  };
}

function initialPlanConfig(
  action: PlanAction,
  fallback: AgentRunConfig | null,
): AgentRunConfig | null {
  const config = action.provider
    ? { provider: action.provider, model: "", reasoning: "", run_on: fallback?.run_on ?? "" }
    : fallback;
  return config
    ? {
        ...config,
        model: action.model ?? config.model,
        reasoning: action.reasoning ?? config.reasoning,
      }
    : null;
}
