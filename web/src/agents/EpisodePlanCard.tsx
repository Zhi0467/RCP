import { FlaskConical, Pencil, Telescope } from "lucide-react";
import { useState } from "react";
import {
  AgentConfigChip,
  AgentConfigControls,
  launchProviderReady,
  profileRunConfig,
} from "../core/AgentConfigControls";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { buildNotificationLink } from "../core/notificationLinks";
import type {
  AgentQuestion,
  ApprovalCard,
  ApprovalCardResult,
  ProjectSnapshot,
} from "../core/types";
import { AutoResearchFields, AutoResearchWorktree } from "../experiments/AutoResearchControls";
import { worktreeRepositoryChoices, chosenWorktreeRepository } from "../chat/worktreeRepositories";
import { SYNC_BEFORE_START } from "../experiments/experimentStart";
import { formatNodeContext, formatNodeValue } from "../graph/GlossaryText";
import type { GlossaryIndex } from "../graph/glossary";
import { graphProfile, refsProfile } from "../graph/graphResources";
import { requestNodeEdit } from "../graph/nodeEditRequest";
import { hasStagedHumanDraft } from "../graph/projectSession";
import { ApprovalCardShell, askedCardState, type Busy } from "./ApprovalCard";
import { approvalCardTitle } from "./agentThreadModel";
import {
  autoResearchInputsReady,
  autoResearchStartDecision,
  initialAutoResearchInputs,
  type AutoResearchAction,
  type AutoResearchInputs,
  initialPlanInputs,
  planCardAction,
  planGraphTarget,
  planInputsReady,
  planStartDecision,
  planTurnCap,
  tapPlanCard,
  type ExperimentStartAction,
  type PlanInputs,
} from "./experimentPlanModel";

export interface CardProps {
  question: AgentQuestion;
  glossaryIndex?: GlossaryIndex;
  onOpenNode?: (nodeId: string) => void;
  onSettled: (result: ApprovalCardResult | null) => void;
  branchNamesInUse?: readonly string[];
}
export function EpisodePlanCard({
  question,
  actorId,
  ...props
}: CardProps & { actorId: string | null }) {
  const action = planCardAction(question.card)!;
  const target = planGraphTarget(action);
  const profile = graphProfile(action.project_id, target);
  const graph = useResource(`experiment-plan:${question.question_id}`, profile);
  const refs = useResource(
    `plan-branches:${question.question_id}`,
    action.kind === "auto_research" ? refsProfile(action.project_id) : null,
  );
  const { store } = useRefreshRuntime();
  return (
    <EpisodePlanCardView
      {...props}
      question={question}
      snapshot={graph.value ?? null}
      branchNamesInUse={(refs.value ?? []).map((ref) => ref.name)}
      staged={() => hasStagedHumanDraft(localStorage, actorId, action.project_id, target)}
      onPlanChanged={() => store.invalidate(resourceKeyId(profile.key))}
    />
  );
}

function ExperimentPlanCardView({
  question,
  snapshot,
  staged,
  glossaryIndex,
  onOpenNode,
  onPlanChanged,
  onSettled,
  resolve,
}: CardProps & {
  snapshot: Pick<
    ProjectSnapshot,
    "graph" | "agent_profiles" | "run_on" | "provider_readiness"
  > | null;
  staged: () => boolean;
  onPlanChanged: () => void;
  resolve?: Parameters<typeof tapPlanCard>[2];
}) {
  const card = question.card as ApprovalCard;
  const action = planCardAction(card) as ExperimentStartAction;
  const node = snapshot?.graph.nodes[action.node_id] ?? null;
  const launchProfile = snapshot?.agent_profiles.node_chat;
  const fallback =
    launchProfile && snapshot ? profileRunConfig(launchProfile, snapshot.run_on) : null;
  const [edits, setEdits] = useState<Partial<PlanInputs>>({});
  const inputs: PlanInputs = { ...initialPlanInputs(action, fallback), ...edits };
  const [editingGoal, setEditingGoal] = useState(false);
  const [agentOpen, setAgentOpen] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [error, setError] = useState<string | null>(null);
  const { pending, state, stateLabel } = askedCardState(question, card);
  const cap = planTurnCap(action, node);
  const isStaged = pending && staged();
  const workLike = launchProfile?.work_like_capable;
  const providerReady =
    !snapshot || !inputs.config || launchProviderReady(snapshot, inputs.config, workLike);
  const locked = !question.can_answer || busy !== null || card.state === "running";
  const target = planGraphTarget(action);
  const openNode = (edit: boolean) => {
    if (edit)
      requestNodeEdit({
        projectId: action.project_id,
        graphTarget: target,
        nodeId: action.node_id,
      });
    if (onOpenNode) onOpenNode(action.node_id);
    else
      window.location.assign(
        buildNotificationLink({
          projectId: action.project_id,
          target: target.kind === "main" ? "main" : target.branch_id,
          kind: "node",
          itemId: action.node_id,
        }),
      );
  };
  const tap = async (decision: "approve" | "decline") => {
    if (decision === "approve" && staged()) return setError(SYNC_BEFORE_START);
    if (decision === "approve" && !node) return;
    setBusy(decision);
    setError(null);
    const outcome = await tapPlanCard(
      question.question_id,
      decision === "approve"
        ? planStartDecision(card, inputs, node!)
        : { decision, pinned_sha256: card.pinned_sha256 },
      resolve,
    );
    setBusy(null);
    if (outcome.kind === "pending") {
      setError(outcome.reason);
      if (outcome.planChanged) onPlanChanged();
    }
    onSettled(outcome.kind === "settled" ? outcome.result : null);
  };
  const editIcon = (label: string) =>
    question.can_answer && (
      <button
        type="button"
        className="plan-edit"
        aria-label={label}
        title={label}
        onClick={() => openNode(true)}
      >
        <Pencil size={12} aria-hidden="true" />
      </button>
    );
  const chip = (
    <button type="button" className="entity-chip" onClick={() => openNode(false)}>
      <FlaskConical size={11} aria-hidden="true" />
      <span>{action.node_id}</span>
    </button>
  );
  const body = (
    <div className="approval-details plan-body">
      {node && <h4>{node.title}</h4>}
      <dl className="plan-section">
        <dt>
          Design
          {editIcon("Edit node")}
        </dt>
        <dd>{node?.design ? formatNodeContext(node.design, glossaryIndex) : null}</dd>
      </dl>
      <dl className="plan-section">
        <dt>
          Done when
          {editIcon("Edit node")}
        </dt>
        <dd>
          {node?.completion_criteria?.length
            ? formatNodeValue(node.completion_criteria, glossaryIndex)
            : null}
        </dd>
      </dl>
      <dl className="plan-section run">
        <dt>
          This run
          {question.can_answer && !editingGoal && (
            <button
              type="button"
              className="plan-edit"
              aria-label="Edit goal"
              title="Edit goal"
              disabled={locked}
              onClick={() => setEditingGoal(true)}
            >
              <Pencil size={12} aria-hidden="true" />
            </button>
          )}
        </dt>
        <dd>
          {editingGoal ? (
            <textarea
              className="plan-goal-input"
              aria-label="Goal"
              value={inputs.goal}
              disabled={locked}
              rows={Math.min(12, Math.max(3, inputs.goal.split("\n").length + 1))}
              autoFocus
              onChange={(event) => setEdits({ ...edits, goal: event.target.value })}
              onBlur={() => setEditingGoal(false)}
            />
          ) : (
            <p className="plan-goal">{inputs.goal}</p>
          )}
        </dd>
      </dl>
      <div className="plan-launch">
        {snapshot && inputs.config && (
          <AgentConfigChip
            project={snapshot}
            value={inputs.config}
            effectiveModel={
              launchProfile && inputs.config.provider === launchProfile.provider
                ? launchProfile.effective_model
                : ""
            }
            open={agentOpen}
            disabled={locked}
            label="Experiment agent"
            workLike={workLike}
            onToggle={() => setAgentOpen(!agentOpen)}
          />
        )}
        <label className="plan-turns" title={`At most ${cap}`}>
          <input
            type="number"
            min={1}
            max={cap}
            step={1}
            value={Number.isFinite(inputs.invocationCeiling) ? inputs.invocationCeiling : ""}
            disabled={locked}
            aria-label="Turn limit"
            onChange={(event) =>
              setEdits({ ...edits, invocationCeiling: event.target.valueAsNumber })
            }
          />
          turns
        </label>
      </div>
      {agentOpen && snapshot && inputs.config && (
        <AgentConfigControls
          project={snapshot}
          value={inputs.config}
          onChange={(config) => setEdits({ ...edits, config })}
          effectiveModel={
            launchProfile && inputs.config.provider === launchProfile.provider
              ? launchProfile.effective_model
              : ""
          }
          workLikeCapable={workLike}
          locked={locked}
          compact
        />
      )}
      {isStaged && (
        <p className="plan-note" role="status">
          {SYNC_BEFORE_START}
        </p>
      )}
    </div>
  );
  return (
    <ApprovalCardShell
      icon={FlaskConical}
      title={approvalCardTitle(card)}
      className="plan"
      state={state}
      stateLabel={stateLabel}
      project={null}
      chip={chip}
      askedAt={question.created_at}
      body={pending ? body : undefined}
      approveLabel="Start"
      declineLabel="Don't run this now"
      approveDisabled={locked || isStaged || !providerReady || !planInputsReady(inputs, node, cap)}
      declineDisabled={!question.can_answer}
      error={error}
      busy={busy}
      running={busy !== null || card.state === "running"}
      onApprove={() => void tap("approve")}
      onDecline={() => void tap("decline")}
    />
  );
}

type ViewProps = Parameters<typeof ExperimentPlanCardView>[0];

export function EpisodePlanCardView(
  props: ViewProps & {
    snapshot:
      | (ViewProps["snapshot"] &
          Partial<Pick<ProjectSnapshot, "repositories" | "project_truth_scope">>)
      | null;
  },
) {
  return props.question.card?.action.kind === "auto_research" ? (
    <AutoResearchPlanCardView {...props} />
  ) : (
    <ExperimentPlanCardView {...props} />
  );
}

function AutoResearchPlanCardView({
  question,
  snapshot,
  onSettled,
  resolve,
  branchNamesInUse,
}: Parameters<typeof EpisodePlanCardView>[0]) {
  const card = question.card as ApprovalCard;
  const action = card.action as AutoResearchAction;
  const profile = snapshot?.agent_profiles.orchestrator;
  const fallback = profile && snapshot ? profileRunConfig(profile, snapshot.run_on) : null;
  const [edits, setEdits] = useState<Partial<AutoResearchInputs>>({});
  const inputs = { ...initialAutoResearchInputs(action, fallback), ...edits };
  const [agentOpen, setAgentOpen] = useState(false);
  const [busy, setBusy] = useState<Busy>(null);
  const [branchRefusal, setBranchRefusal] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const { pending, state, stateLabel } = askedCardState(question, card);
  const locked = !question.can_answer || busy !== null || card.state === "running";
  const repositories =
    snapshot?.repositories && snapshot.project_truth_scope && inputs.config
      ? worktreeRepositoryChoices(
          {
            repositories: snapshot.repositories,
            project_truth_scope: snapshot.project_truth_scope,
          },
          inputs.config.run_on,
        )
      : null;
  const repository = repositories
    ? chosenWorktreeRepository(repositories, inputs.worktreeRepository, null)
    : null;
  const unavailable = repositories !== null && repository === null;
  const codeWorktree = inputs.codeWorktree && !unavailable;
  const effectiveModel =
    profile && inputs.config?.provider === profile.provider ? profile.effective_model : "";
  const providerReady =
    snapshot &&
    inputs.config &&
    launchProviderReady(snapshot, inputs.config, profile?.work_like_capable);
  const tap = async (decision: "approve" | "decline") => {
    setBusy(decision);
    setError(null);
    setBranchRefusal(null);
    const outcome = await tapPlanCard(
      question.question_id,
      decision === "approve"
        ? autoResearchStartDecision(card, {
            ...inputs,
            codeWorktree,
            worktreeRepository: repository,
          })
        : { decision, pinned_sha256: card.pinned_sha256 },
      resolve,
    );
    setBusy(null);
    if (outcome.kind === "pending") {
      setBranchRefusal(outcome.branchRefusal ?? null);
      if (!outcome.branchRefusal) setError(outcome.reason);
    }
    onSettled(outcome.kind === "settled" ? outcome.result : null);
  };
  const body = (
    <div className="approval-details plan-body">
      <div className="campaign-dialog-fields">
        <AutoResearchFields
          branchNamesInUse={branchNamesInUse}
          branchNameRefusal={branchRefusal}
          instructionRequired
          budget={Number.isFinite(inputs.invocationCeiling) ? String(inputs.invocationCeiling) : ""}
          instruction={inputs.instruction}
          branch={inputs.branch}
          busy={locked}
          setBudget={(value) =>
            setEdits({ ...edits, invocationCeiling: value === "" ? NaN : Number(value) })
          }
          setInstruction={(instruction) => setEdits({ ...edits, instruction })}
          setBranch={(branch) => setEdits({ ...edits, branch })}
        />
        <div className="agent-config-options campaign-isolation">
          <AutoResearchWorktree
            worktreeUnavailable={unavailable}
            runOn={inputs.config?.run_on}
            codeWorktreeOn={codeWorktree}
            busy={locked}
            worktreeRepositories={repositories}
            worktreeRepository={repository}
            setCodeWorktree={(codeWorktree) => setEdits({ ...edits, codeWorktree })}
            setWorktreePick={(worktreeRepository) => setEdits({ ...edits, worktreeRepository })}
          />
        </div>
      </div>
      {snapshot && inputs.config && (
        <AgentConfigChip
          project={snapshot}
          value={inputs.config}
          effectiveModel={effectiveModel}
          open={agentOpen}
          disabled={locked}
          label="Orchestrator agent"
          workLike={profile?.work_like_capable}
          onToggle={() => setAgentOpen(!agentOpen)}
        />
      )}
      {agentOpen && snapshot && inputs.config && (
        <AgentConfigControls
          project={snapshot}
          value={inputs.config}
          onChange={(config) => setEdits({ ...edits, config })}
          effectiveModel={effectiveModel}
          workLikeCapable={profile?.work_like_capable}
          locked={locked}
          compact
        />
      )}
    </div>
  );
  return (
    <ApprovalCardShell
      icon={Telescope}
      title={approvalCardTitle(card)}
      className="plan"
      state={state}
      stateLabel={stateLabel}
      project={null}
      askedAt={question.created_at}
      body={pending ? body : undefined}
      approveLabel="Start"
      declineLabel="Don't run this now"
      approveDisabled={locked || !providerReady || !autoResearchInputsReady(inputs)}
      declineDisabled={!question.can_answer}
      error={error}
      busy={busy}
      running={busy !== null || card.state === "running"}
      onApprove={() => void tap("approve")}
      onDecline={() => void tap("decline")}
    />
  );
}
