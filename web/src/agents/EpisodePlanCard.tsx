import { FlaskConical, GitBranch, Pencil, Telescope } from "lucide-react";
import {
  createContext,
  useContext,
  useEffect,
  useEffectEvent,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { launchProviderReady, profileRunConfig } from "../core/AgentConfigControls";
import { useAgentConfigPicker } from "../core/AgentConfigPicker";
import { errorMessage } from "../core/errors";
import { resourceKeyId } from "../core/resourceStore";
import { useRefreshRuntime, useResource } from "../core/useResource";
import { buildNotificationLink } from "../core/notificationLinks";
import type {
  AgentQuestion,
  AgentRunConfig,
  ApprovalCard,
  ApprovalCardResult,
  ProjectSnapshot,
} from "../core/types";
import { AutoResearchWorktree } from "../experiments/AutoResearchControls";
import { BranchNameField } from "../graph/BranchNameField";
import { worktreeRepositoryChoices, chosenWorktreeRepository } from "../chat/worktreeRepositories";
import { SYNC_BEFORE_START } from "../experiments/experimentStart";
import { formatNodeContext, formatNodeValue } from "../graph/GlossaryText";
import type { GlossaryIndex } from "../graph/glossary";
import { graphProfile, refsProfile } from "../graph/graphResources";
import { hasStagedHumanDraft } from "../graph/projectSession";
import { relativeTime } from "../projects/commentMentions";
import { ApprovalCardShell } from "./ApprovalCard";
import { approvalCardTitle } from "./agentThreadModel";
import {
  autoResearchInputsReady,
  autoResearchStartDecision,
  criteriaLines,
  initialAutoResearchInputs,
  type AutoResearchAction,
  type AutoResearchInputs,
  initialPlanInputs,
  nodeDesign,
  planCardAction,
  planGraphTarget,
  planInputsReady,
  planNodeChanges,
  planNodeDraftStale,
  planStartDecision,
  planTurnCap,
  savePlanNodeEdit,
  tapPlanCard,
  type ExperimentStartAction,
  type PlanDecisionKind,
  type PlanGraphPorts,
  type PlanInputs,
  type PlanNodeDraft,
  type PlanResolve,
  type PlanTapOutcome,
} from "./experimentPlanModel";
import { taskCardView } from "./taskCardModel";

type PlanDraft = Partial<PlanInputs & AutoResearchInputs> & PlanNodeDraft;
/** One open plan card's draft, the hash it was read under, and the hash this tab's own save returned. */
interface PlanMemory {
  draft: PlanDraft;
  seen?: string;
  adopted?: string;
  adoptedFrom?: string;
}
const NO_MEMORY: PlanMemory = { draft: {} };
const PlanDrafts = createContext<{
  memories: ReadonlyMap<string, PlanMemory>;
  set: (questionId: string, memory: PlanMemory) => void;
  onParked?: (questionId: string) => void;
} | null>(null);

/**
 * Holds every open plan card's draft for one agent view, so a draft survives its
 * card moving or folding. A provider inside another hands its cards to the outer one.
 */
export function EpisodePlanDraftProvider({
  questionIds,
  onParked,
  children,
}: {
  questionIds: readonly string[];
  /** A card was parked: the view sends its copy to the pane button. */
  onParked?: (questionId: string) => void;
  children: ReactNode;
}) {
  const outer = useContext(PlanDrafts);
  const [memories, setMemories] = useState<ReadonlyMap<string, PlanMemory>>(() => new Map());
  const active = questionIds.join("\n");
  useEffect(() => {
    const open = new Set(active.split("\n"));
    setMemories((known) => {
      const next = new Map([...known].filter(([id]) => open.has(id)));
      return next.size === known.size ? known : next;
    });
  }, [active]);
  return (
    <PlanDrafts.Provider
      value={
        outer ?? {
          memories,
          set: (id, memory) => setMemories((known) => new Map(known).set(id, memory)),
          onParked,
        }
      }
    >
      {children}
    </PlanDrafts.Provider>
  );
}

/**
 * A card's draft and the hash its taps send. A hash this tab did not write
 * drops the draft: the agent's or another member's revision wins.
 */
function usePlanMemory(questionId: string, card: ApprovalCard) {
  const shared = useContext(PlanDrafts);
  const [local, setLocal] = useState<PlanMemory>(NO_MEMORY);
  const memory = shared ? (shared.memories.get(questionId) ?? NO_MEMORY) : local;
  const update = (next: PlanMemory) => (shared ? shared.set(questionId, next) : setLocal(next));
  const shown = card.pinned_sha256;
  const awaitingOwnSave = memory.adopted !== undefined && shown === memory.adoptedFrom;
  const see = useEffectEvent(() => {
    if (memory.seen === shown || awaitingOwnSave) return;
    const own = memory.seen === undefined || memory.adopted === shown;
    update({ ...memory, draft: own ? memory.draft : {}, seen: shown, adoptedFrom: undefined });
  });
  useEffect(() => see(), [shown, awaitingOwnSave]);
  return {
    draft: memory.draft,
    sha: awaitingOwnSave ? memory.adopted! : shown,
    ownRevision: memory.adopted === shown,
    setDraft: (draft: PlanDraft) => update({ ...memory, draft }),
    adopt: (sha: string) => update({ draft: {}, seen: sha, adopted: sha, adoptedFrom: shown }),
    parked: () => shared?.onParked?.(questionId),
  };
}

function withoutNodeDraft(draft: PlanDraft): PlanDraft {
  const { design: _design, doneWhen: _doneWhen, nodeRev: _nodeRev, ...rest } = draft;
  return rest;
}

type PlanSnapshot = Pick<
  ProjectSnapshot,
  "graph" | "agent_profiles" | "run_on" | "provider_readiness"
>;

const HANDOFF_EDITOR_LINES = 16;

function GrowingTextarea({
  label,
  value,
  disabled,
  autoFocus,
  onChange,
}: {
  label: string;
  value: string;
  disabled: boolean;
  autoFocus: boolean;
  onChange: (value: string) => void;
}) {
  const ref = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    element.style.height = "auto";
    const line = parseFloat(getComputedStyle(element).lineHeight) || 20;
    const frame = element.offsetHeight - element.clientHeight;
    element.style.height = `${Math.min(element.scrollHeight, line * HANDOFF_EDITOR_LINES + 12) + frame}px`;
  }, [value]);
  return (
    <textarea
      ref={ref}
      className="plan-goal-input"
      aria-label={label}
      value={value}
      disabled={disabled}
      rows={3}
      autoFocus={autoFocus}
      onChange={(event) => onChange(event.target.value)}
    />
  );
}

/** The handoff text as read: six lines, then a fade and Show more. */
export function HandoffText({ text }: { text: string }) {
  const ref = useRef<HTMLParagraphElement>(null);
  const [expanded, setExpanded] = useState(false);
  const [long, setLong] = useState(false);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element || expanded) return;
    const measure = () => setLong(element.scrollHeight > element.clientHeight + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    return () => observer.disconnect();
  }, [text, expanded]);
  return (
    <>
      <p
        ref={ref}
        className="plan-goal plan-handoff"
        data-clamped={expanded ? undefined : ""}
        data-long={long && !expanded ? "" : undefined}
      >
        {text}
      </p>
      {long && (
        <button
          type="button"
          className="plan-more"
          aria-expanded={expanded}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
    </>
  );
}

function PlanField({
  title,
  editLabel,
  inputLabel,
  value,
  read,
  canEdit,
  locked,
  open,
  className,
  onChange,
  children,
}: {
  title: string;
  editLabel: string;
  inputLabel: string;
  value: string;
  read: ReactNode;
  canEdit: boolean;
  locked: boolean;
  open?: boolean;
  className?: string;
  onChange: (value: string) => void;
  children?: ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const shown = editing || Boolean(open);
  const edit = setEditing;
  return (
    <dl className={`plan-section${className ? ` ${className}` : ""}`}>
      <dt>
        {title}
        {canEdit && !shown && (
          <button
            type="button"
            className="plan-edit"
            aria-label={editLabel}
            title={editLabel}
            disabled={locked}
            onClick={() => edit(true)}
          >
            <Pencil size={12} aria-hidden="true" />
          </button>
        )}
      </dt>
      <dd
        onBlur={(event) => {
          const next = event.relatedTarget as Node | null;
          const card = event.currentTarget.closest(".approval-card");
          if (next && !card?.contains(next)) edit(false);
        }}
      >
        {shown ? (
          <>
            <GrowingTextarea
              label={inputLabel}
              value={value}
              disabled={locked}
              autoFocus={editing}
              onChange={onChange}
            />
            {children}
          </>
        ) : (
          read
        )}
      </dd>
    </dl>
  );
}

function PlanLaunch({
  snapshot,
  profile,
  config,
  agentLabel,
  turns,
  cap,
  locked,
  onTurns,
  onConfig,
  children,
}: {
  snapshot: PlanSnapshot | null;
  profile: ProjectSnapshot["agent_profiles"]["node_chat"] | undefined;
  config: AgentRunConfig | null;
  agentLabel: string;
  turns: number;
  cap?: number;
  locked: boolean;
  onTurns: (turns: number) => void;
  onConfig: (config: AgentRunConfig) => void;
  children?: ReactNode;
}) {
  const effectiveModel =
    profile && config?.provider === profile.provider ? profile.effective_model : "";
  const picker = useAgentConfigPicker({
    project: snapshot ?? undefined,
    value: config ?? undefined,
    onChange: onConfig,
    label: agentLabel,
    disabled: locked,
    effectiveModel,
    workLike: profile?.work_like_capable,
    workLikeCapable: profile?.work_like_capable,
  });
  return (
    <>
      <div className="plan-launch">
        {picker.chip}
        <label className="plan-turns" title={cap ? `At most ${cap}` : undefined}>
          <input
            type="number"
            min={1}
            max={cap}
            step={1}
            value={Number.isFinite(turns) ? turns : ""}
            disabled={locked}
            aria-label="Turn limit"
            style={{ width: `${String(Number.isFinite(turns) ? turns : "").length + 1}ch` }}
            onChange={(event) => onTurns(event.target.valueAsNumber)}
          />
          turns
        </label>
        {children}
      </div>
      {picker.panel}
    </>
  );
}

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

/** The Experiment a plan is for; a press opens the node for reading. */
export function PlanNodeChip({
  action,
  onOpenNode,
}: {
  action: ExperimentStartAction;
  onOpenNode?: (nodeId: string) => void;
}) {
  const target = planGraphTarget(action);
  const open = () => {
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
  return (
    <button type="button" className="entity-chip" onClick={open}>
      <FlaskConical size={11} aria-hidden="true" />
      <span>{action.node_id}</span>
    </button>
  );
}

type PlanBusy = PlanDecisionKind | null;

/** The line a revision by someone else leaves on the card. */
function RevisedNote({ card, own }: { card: ApprovalCard; own: boolean }) {
  if (!card.revised_at || !card.revised_by || own) return null;
  const name =
    card.revised_by.name ?? (card.revised_by.kind === "agent" ? "The agent" : "A member");
  return (
    <p className="plan-note" role="status">
      {name} changed this plan{" "}
      <time dateTime={card.revised_at} title={new Date(card.revised_at).toLocaleString()}>
        {relativeTime(card.revised_at)}
      </time>
      .
    </p>
  );
}

/** A plan card's fold, its taps, and what each outcome leaves on the card. */
function usePlanTaps(
  question: AgentQuestion,
  plan: ReturnType<typeof usePlanMemory>,
  onSettled: CardProps["onSettled"],
  resolve: PlanResolve | undefined,
) {
  const card = question.card as ApprovalCard;
  const [busy, setBusy] = useState<PlanBusy>(null);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [parkedHere, setParkedHere] = useState(false);
  const view = taskCardView(card);
  const parked = view.phase === "parked" || parkedHere;
  const send = async (
    decision: PlanDecisionKind,
    request: Parameters<typeof tapPlanCard>[1],
  ): Promise<PlanTapOutcome> => {
    const outcome = await tapPlanCard(question.question_id, request, resolve);
    setBusy(null);
    if (outcome.kind === "kept") {
      plan.adopt(outcome.revision.pinned_sha256);
      if (decision === "park") {
        setExpanded(false);
        setParkedHere(true);
        plan.parked();
      }
    }
    onSettled(outcome.kind === "settled" ? outcome.result : null);
    return outcome;
  };
  return {
    card,
    busy,
    setBusy,
    error,
    setError,
    parked,
    folded: parked && !expanded,
    label: parked ? "Parked" : view.label,
    unfold: () => setExpanded(true),
    locked: !question.can_answer || busy !== null || card.state === "running",
    send,
  };
}

function PlanActions({
  parked,
  dirty,
  busy,
  locked,
  startDisabled,
  startNote,
  canAnswer,
  runHere,
  onTap,
}: {
  parked: boolean;
  dirty: boolean;
  busy: PlanBusy;
  locked: boolean;
  startDisabled: boolean;
  /** What Start also does, shown on hover. */
  startNote?: string;
  canAnswer: boolean;
  runHere: boolean;
  onTap: (decision: PlanDecisionKind) => void;
}) {
  const quiet = (decision: PlanDecisionKind, label: string, disabled = false) => (
    <button
      className="button secondary"
      type="button"
      disabled={locked || !canAnswer || disabled}
      aria-busy={busy === decision || undefined}
      onClick={() => onTap(decision)}
    >
      {label}
    </button>
  );
  return (
    <>
      {quiet("decline", parked ? "Delete" : "Don't run")}
      {parked ? dirty && quiet("save", "Save") : quiet("park", "Park for later")}
      {runHere && !parked && (
        <button
          className="button"
          type="button"
          disabled={startDisabled}
          aria-busy={busy === "run_here" || undefined}
          onClick={() => onTap("run_here")}
        >
          Run here
        </button>
      )}
      <button
        className="button primary"
        type="button"
        disabled={startDisabled}
        title={startNote}
        aria-busy={busy === "approve" || undefined}
        onClick={() => onTap("approve")}
      >
        Start
      </button>
    </>
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
  graph,
}: CardProps & {
  snapshot: PlanSnapshot | null;
  staged: () => boolean;
  onPlanChanged: () => void;
  resolve?: PlanResolve;
  graph?: PlanGraphPorts;
}) {
  const card = question.card as ApprovalCard;
  const action = planCardAction(card) as ExperimentStartAction;
  const node = snapshot?.graph.nodes[action.node_id] ?? null;
  const launchProfile = snapshot?.agent_profiles.node_chat;
  const fallback =
    launchProfile && snapshot ? profileRunConfig(launchProfile, snapshot.run_on) : null;
  const plan = usePlanMemory(question.question_id, card);
  const edits = plan.draft;
  const setEdits = plan.setDraft;
  const taps = usePlanTaps(question, plan, onSettled, resolve);
  const { busy, locked, setBusy, setError } = taps;
  const initial = initialPlanInputs(action, fallback);
  const inputs: PlanInputs = {
    goal: edits.goal ?? initial.goal,
    invocationCeiling: edits.invocationCeiling ?? initial.invocationCeiling,
    config: edits.config ?? initial.config,
  };
  const [nodeMoves, setNodeMoves] = useState(0);
  const nodeMoved = () => setNodeMoves((count) => count + 1);
  const stale = planNodeDraftStale(edits, node);
  const dropStale = useEffectEvent(() => {
    setEdits(withoutNodeDraft(edits));
    nodeMoved();
  });
  useEffect(() => {
    if (stale) dropStale();
  }, [stale]);
  const nodeChanges = planNodeChanges(edits, node);
  const cap = planTurnCap(action, node);
  const isStaged = !taps.folded && staged();
  const workLike = launchProfile?.work_like_capable;
  const providerReady =
    !snapshot || !inputs.config || launchProviderReady(snapshot, inputs.config, workLike);
  const target = planGraphTarget(action);
  const editNode = (change: PlanNodeDraft) =>
    setEdits({ ...edits, ...change, nodeRev: edits.nodeRev ?? node?.updated_rev });
  const tap = async (decision: PlanDecisionKind) => {
    const shown = { ...card, pinned_sha256: plan.sha };
    if (decision === "decline") {
      setBusy(decision);
      setError(null);
      await taps.send(decision, { decision, pinned_sha256: plan.sha });
      return;
    }
    if (decision !== "park" && decision !== "save" && staged()) return setError(SYNC_BEFORE_START);
    if (!node) return;
    setBusy(decision);
    setError(null);
    let nodeRev = node.updated_rev;
    if (nodeChanges) {
      try {
        const saved = await savePlanNodeEdit(
          {
            projectId: action.project_id,
            target,
            nodeId: action.node_id,
            baseUpdatedRev: edits.nodeRev ?? node.updated_rev,
            changes: nodeChanges,
          },
          graph,
        );
        setEdits(withoutNodeDraft(edits));
        onPlanChanged();
        if (saved.kind === "conflict") {
          nodeMoved();
          setBusy(null);
          return;
        }
        nodeRev = saved.nodeRev;
      } catch (failure) {
        setError(errorMessage(failure));
        setBusy(null);
        return;
      }
    }
    const outcome = await taps.send(
      decision,
      planStartDecision(shown, inputs, { updated_rev: nodeRev }, decision),
    );
    if (outcome.kind === "pending") {
      setError(outcome.reason);
      if (outcome.planChanged) onPlanChanged();
    }
  };
  const criteria =
    edits.doneWhen !== undefined
      ? criteriaLines(edits.doneWhen)
      : (node?.completion_criteria ?? []);
  const dirty =
    nodeChanges !== null ||
    JSON.stringify(planStartDecision(card, inputs, { updated_rev: 0 }).start) !==
      JSON.stringify(planStartDecision(card, initial, { updated_rev: 0 }).start);
  const body = (
    <div className="approval-details plan-body">
      {node && <h4>{node.title}</h4>}
      <PlanField
        key={`design:${nodeMoves}`}
        title="Design"
        editLabel="Edit design"
        inputLabel="Design"
        value={edits.design ?? nodeDesign(node)}
        read={
          edits.design !== undefined ? (
            <p className="plan-goal">{edits.design}</p>
          ) : node?.design ? (
            formatNodeContext(node.design, glossaryIndex)
          ) : null
        }
        canEdit={question.can_answer && node !== null}
        locked={locked}
        onChange={(design) => editNode({ design })}
      />
      <PlanField
        key={`done-when:${nodeMoves}`}
        title="Done when"
        editLabel="Edit done when"
        inputLabel="Done when"
        value={edits.doneWhen ?? (node?.completion_criteria ?? []).join("\n")}
        read={criteria.length ? formatNodeValue(criteria, glossaryIndex) : null}
        canEdit={question.can_answer && node !== null}
        locked={locked}
        onChange={(doneWhen) => editNode({ doneWhen })}
      />
      <PlanField
        title="Handoff"
        className="run"
        editLabel="Edit handoff"
        inputLabel="Handoff"
        value={inputs.goal}
        read={<HandoffText text={inputs.goal} />}
        canEdit={question.can_answer}
        locked={locked}
        onChange={(goal) => setEdits({ ...edits, goal })}
      />
      <PlanLaunch
        snapshot={snapshot}
        profile={launchProfile}
        config={inputs.config}
        agentLabel="Experiment agent"
        turns={inputs.invocationCeiling}
        cap={cap}
        locked={locked}
        onTurns={(invocationCeiling) => setEdits({ ...edits, invocationCeiling })}
        onConfig={(config) => setEdits({ ...edits, config })}
      />
      {isStaged && (
        <p className="plan-note" role="status">
          {SYNC_BEFORE_START}
        </p>
      )}
      {nodeMoves > 0 && (
        <p className="plan-note" role="status">
          The node changed. Design and Done when show its new text.
        </p>
      )}
      <RevisedNote card={card} own={plan.ownRevision} />
    </div>
  );
  return (
    <ApprovalCardShell
      icon={FlaskConical}
      title={approvalCardTitle(card)}
      className={`plan${taps.parked ? " parked" : ""}${taps.folded ? " folded" : ""}`}
      cardId={question.question_id}
      state="pending"
      stateLabel={taps.label}
      project={null}
      chip={<PlanNodeChip action={action} onOpenNode={onOpenNode} />}
      askedAt={question.created_at}
      body={taps.folded ? undefined : body}
      onOpen={taps.folded ? taps.unfold : undefined}
      error={taps.error}
      actions={
        taps.folded ? null : (
          <PlanActions
            parked={taps.parked}
            dirty={dirty}
            busy={busy}
            locked={locked}
            canAnswer={question.can_answer}
            runHere
            startNote={nodeChanges ? "Saves your edits to the node" : undefined}
            startDisabled={
              locked || isStaged || !providerReady || !planInputsReady(inputs, node, cap)
            }
            onTap={(decision) => void tap(decision)}
          />
        )
      }
    />
  );
}

type ViewProps = Parameters<typeof ExperimentPlanCardView>[0];

/** An open plan card: its editor while it waits for the member, folded to one line while parked. */
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
  const plan = usePlanMemory(question.question_id, card);
  const edits = plan.draft;
  const setEdits = plan.setDraft;
  const taps = usePlanTaps(question, plan, onSettled, resolve);
  const { busy, locked, setBusy, setError } = taps;
  const initial = initialAutoResearchInputs(action, fallback);
  const inputs = { ...initial, ...edits };
  const [branchRefusal, setBranchRefusal] = useState<string | null>(null);
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
  const providerReady =
    snapshot &&
    inputs.config &&
    launchProviderReady(snapshot, inputs.config, profile?.work_like_capable);
  const tap = async (decision: PlanDecisionKind) => {
    setBusy(decision);
    setError(null);
    setBranchRefusal(null);
    const outcome = await taps.send(
      decision,
      decision === "decline"
        ? { decision, pinned_sha256: plan.sha }
        : autoResearchStartDecision(
            { ...card, pinned_sha256: plan.sha },
            { ...inputs, codeWorktree, worktreeRepository: repository },
            decision,
          ),
    );
    if (outcome.kind === "pending") {
      setBranchRefusal(outcome.branchRefusal ?? null);
      if (!outcome.branchRefusal) setError(outcome.reason);
    }
  };
  const body = (
    <div className="approval-details plan-body">
      <PlanField
        title="Handoff"
        className="run"
        editLabel="Edit instruction"
        inputLabel="Starting instruction"
        value={inputs.instruction}
        read={<HandoffText text={inputs.instruction} />}
        canEdit={question.can_answer}
        locked={locked}
        open={branchRefusal !== null}
        onChange={(instruction) => setEdits({ ...edits, instruction })}
      >
        <div className="campaign-isolation">
          <BranchNameField
            mode="auto_research"
            value={inputs.branch}
            disabled={locked}
            refusal={branchRefusal}
            namesInUse={branchNamesInUse}
            onChange={(branch) => setEdits({ ...edits, branch })}
          />
        </div>
      </PlanField>
      <PlanLaunch
        snapshot={snapshot}
        profile={profile}
        config={inputs.config}
        agentLabel="Orchestrator agent"
        turns={inputs.invocationCeiling}
        locked={locked}
        onTurns={(invocationCeiling) => setEdits({ ...edits, invocationCeiling })}
        onConfig={(config) => setEdits({ ...edits, config })}
      >
        <span className="campaign-isolation">
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
        </span>
      </PlanLaunch>
      <RevisedNote card={card} own={plan.ownRevision} />
    </div>
  );
  return (
    <ApprovalCardShell
      icon={Telescope}
      title={approvalCardTitle(card)}
      className={`plan${taps.parked ? " parked" : ""}${taps.folded ? " folded" : ""}`}
      cardId={question.question_id}
      state="pending"
      stateLabel={taps.label}
      project={null}
      chip={
        inputs.branch.trim() ? (
          <span className="entity-chip">
            <GitBranch size={11} aria-hidden="true" />
            <span>{inputs.branch.trim()}</span>
          </span>
        ) : undefined
      }
      askedAt={question.created_at}
      body={taps.folded ? undefined : body}
      onOpen={taps.folded ? taps.unfold : undefined}
      error={taps.error}
      actions={
        taps.folded ? null : (
          <PlanActions
            parked={taps.parked}
            dirty={Object.keys(edits).length > 0}
            busy={busy}
            locked={locked}
            canAnswer={question.can_answer}
            runHere={false}
            startDisabled={locked || !providerReady || !autoResearchInputsReady(inputs)}
            onTap={(decision) => void tap(decision)}
          />
        )
      }
    />
  );
}
