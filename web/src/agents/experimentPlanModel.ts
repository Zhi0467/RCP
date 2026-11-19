import { branchNameRefusal, branchNameRequest, branchNameValid } from "../graph/BranchNameField";
import { ApiError } from "../core/api";
import { errorMessage } from "../core/errors";
import { MAIN_GRAPH } from "../core/graphTarget";
import type {
  AgentQuestion,
  AgentRunConfig,
  ApprovalCard,
  ApprovalCardAction,
  ApprovalCardDecision,
  ApprovalCardResult,
  GraphNode,
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

/** Start's request: the pinned digest plus the human's own inputs and the node revision shown. */
export function planStartDecision(
  card: ApprovalCard,
  inputs: PlanInputs,
  node: GraphNode,
): ApprovalCardDecision {
  return {
    decision: "approve",
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

const PLAN_CHANGED = "The plan changed since you opened it. Read it again, then Start.";

export type PlanTapOutcome =
  /** Settled, or taken by another tap whose outcome arrives with the refresh. */
  | { kind: "settled"; result: ApprovalCardResult | null }
  /** Refused before the card was claimed; it stays open with the reason. */
  | { kind: "pending"; reason: string; planChanged: boolean; branchRefusal?: string };

/** One tap on a plan card. A moved plan or any refusal keeps the card open. */
export async function tapPlanCard(
  questionId: string,
  decision: ApprovalCardDecision,
  resolve: typeof resolveApprovalCard = resolveApprovalCard,
): Promise<PlanTapOutcome> {
  try {
    return { kind: "settled", result: await resolve(questionId, decision) };
  } catch (failure) {
    if (failure instanceof ApiError && failure.code === "card_taken")
      return { kind: "settled", result: null };
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
): ApprovalCardDecision {
  return {
    decision: "approve",
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
