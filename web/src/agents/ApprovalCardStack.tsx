import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  Files,
  Telescope,
  SquareStack,
} from "lucide-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useId,
  useReducer,
  useRef,
  useState,
  type ReactNode,
  type ComponentProps,
} from "react";
import type {
  AgentCardList,
  AgentQuestion,
  ApprovalCardResult,
  AskedApprovalCardView,
  DockEpisode,
  Episode,
  AgentTask,
  ProposalCardView,
  RelayedQuestion,
} from "../core/types";
import { AskedTime, QuestionCard } from "../chat/QuestionCard";
import { retryBackoffMs } from "../core/resourceStore";
import { episodeProjection, episodeReportPreviewUrl } from "../experiments/campaigns";
import { EpisodeControls } from "../experiments/EpisodeControls";
import { EpisodeReportLink } from "../experiments/EpisodeReportLink";
import { EpisodePlanDraftProvider } from "./EpisodePlanCard";
import { useEpisodeActions } from "../experiments/episodeActions";
import { experimentIndexProfile } from "../experiments/episodeResources";
import { useResource } from "../core/useResource";
import { AskedApprovalCard, ProposalApprovalCard } from "./ApprovalCard";
import { readAgentCards, readChatCards } from "./agentThreadsApi";
import { isPlanCardQuestion } from "./experimentPlanModel";
import { useAgentCardsVersion } from "./useAgentThreads";

export function AgentCardsUnavailable() {
  return (
    <span
      className="agent-cards-unavailable"
      role="img"
      aria-label="Approval cards couldn't load. Retrying."
      title="Approval cards couldn't load. Retrying."
    >
      <SquareStack size={16} />
    </span>
  );
}

type RunState = {
  busyAction: string | null;
  taskActionId: string | null;
  error: string | null;
  additionalTurns: string;
  updated?: { source: Episode; episode: Episode };
};
const EMPTY_RUN: RunState = {
  busyAction: null,
  taskActionId: null,
  error: null,
  additionalTurns: "",
};

const EXPIRY_GRACE_MS = 1000;

type Decision = "approved" | "rejected";

export type StackCard =
  | {
      kind: "proposal";
      key: string;
      view: ProposalCardView;
      decided: Decision | null;
      showProject: boolean;
    }
  | { kind: "asked"; key: string; question: AgentQuestion; projectName: string | null }
  | { kind: "question"; key: string; question: RelayedQuestion }
  | { kind: "plan"; key: string; question: AgentQuestion }
  | { kind: "run"; key: string; run: DockEpisode };

/**
 * Where a stack stands: its shown card order, the front card's place and key, the cards
 * Later put away, and whether it ever held more than one card, so its space stays put as
 * it empties.
 */
export type StackMemory = {
  order: string[];
  index: number;
  front?: string;
  later: string[];
  held?: boolean;
};

/**
 * The front card's place: a card that newly asks the human, else the remembered card while
 * it is listed, else the nearest one.
 */
export function stackIndex(
  memory: StackMemory,
  order: readonly string[],
  asking: ReadonlySet<string> = new Set(),
): number {
  const arrived = order.findIndex((key) => asking.has(key) && !memory.order.includes(key));
  if (arrived >= 0) return arrived;
  const kept = memory.front === undefined ? -1 : order.indexOf(memory.front);
  return kept >= 0 ? kept : Math.min(memory.index, Math.max(order.length - 1, 0));
}

export function proposalCardKey(view: ProposalCardView): string {
  return `proposal:${view.project_id}:${view.proposal.id}`;
}

export function askedCardKey(question: AgentQuestion): string {
  return `asked:${question.question_id}`;
}

export function questionCardKey(question: RelayedQuestion): string {
  return `question:${question.question_id}`;
}

function planCardKey(question: AgentQuestion): string {
  return `plan:${question.question_id}`;
}

function runCardKey(run: DockEpisode): string {
  return `run:${run.episode.episode_id}`;
}

/**
 * A chat's dock cards after its Proposal and asked cards, decisions first: pending plan
 * cards, each child episode's open questions, other relayed questions, the runs that
 * need action, then the runs that only report their state.
 */
export function dockStackCards(
  plans: readonly AgentQuestion[],
  episodes: readonly DockEpisode[],
  questions: readonly RelayedQuestion[],
): StackCard[] {
  const runs = (needsAction: boolean) =>
    episodes
      .filter((run) => (run.episode.health === "needs_action") === needsAction)
      .map((run): StackCard => ({ kind: "run", key: runCardKey(run), run }));
  return [
    ...plans.map((question): StackCard => ({ kind: "plan", key: planCardKey(question), question })),
    ...[...episodes.flatMap((run) => run.questions), ...questions].map((question): StackCard => ({
      kind: "question",
      key: questionCardKey(question),
      question,
    })),
    ...runs(true),
    ...runs(false),
  ];
}

/** Surviving cards keep their order, new cards join at the back, settled ones drop. */
export function stackOrder(previous: readonly string[], keys: readonly string[]): string[] {
  const live = new Set(keys);
  const kept = previous.filter((key) => live.has(key));
  const known = new Set(kept);
  return [...kept, ...keys.filter((key) => !known.has(key))];
}

/** Later puts the front card away; once every card is away the stack folds to one line. */
export function stackLater(memory: StackMemory, keys: readonly string[]): StackMemory {
  const later = memory.later.filter((key) => keys.includes(key));
  const order = stackOrder(
    memory.order,
    keys.filter((key) => !later.includes(key)),
  );
  if (!order.length) return { ...memory, order, index: 0, later };
  const index = Math.min(memory.index, order.length - 1);
  const rest = order.filter((_, at) => at !== index);
  return {
    ...memory,
    order: rest,
    index: index >= rest.length ? 0 : index,
    later: [...later, order[index]],
  };
}

/** Each rendered stack's place, per stack key; a reload restores server order. */
const memories = new Map<string, StackMemory>();
const FRESH: StackMemory = { order: [], index: 0, later: [] };

function cardIsOpen(card: StackCard): boolean {
  if (card.kind === "proposal") return card.decided === null;
  if (card.kind === "run") return true;
  const { question } = card;
  return (
    question.state !== "answered" &&
    question.state !== "dismissed" &&
    question.card?.state !== "expired"
  );
}

/**
 * The cards at one place: settled ones as receipt lines, open ones as one stack whose
 * front card shows. Later and the arrows only reorder; neither settles anything.
 */
export function ApprovalCardStack({
  stackKey,
  cards,
  onDecided,
  onResolved,
  onAnswered,
  onChanged,
  renderPlanCard,
}: {
  stackKey: string;
  cards: StackCard[];
  onDecided: (view: ProposalCardView, status: Decision) => void;
  onResolved: (question: AgentQuestion, result: ApprovalCardResult) => void;
  onAnswered: (question: RelayedQuestion) => void;
  onChanged: () => void | Promise<unknown>;
  /** Draws a plan card; the view that owns the stack supplies it. */
  renderPlanCard?: (question: AgentQuestion) => ReactNode;
}) {
  const actions = useEpisodeActions(
    cards.find((card) => card.kind === "run")?.run.episode.project_id ?? null,
  );
  const [runStates, setRunStates] = useState<Record<string, RunState>>({});
  const pending = useRef(new Set<string>());
  const updateRun = (id: string, patch: Partial<RunState>) =>
    setRunStates((states) => ({ ...states, [id]: { ...(states[id] ?? EMPTY_RUN), ...patch } }));
  const perform = async (
    run: DockEpisode,
    field: "busyAction" | "taskActionId",
    key: string,
    act: () => Promise<Episode | AgentTask>,
  ) => {
    const id = run.episode.episode_id;
    if (pending.current.has(id)) return;
    pending.current.add(id);
    updateRun(id, { [field]: key, error: null });
    try {
      const result = await act();
      const episode: Episode =
        "operation_id" in result
          ? {
              ...run.episode,
              tasks: run.episode.tasks.map((task) =>
                task.operation_id === result.operation_id ? { ...task, ...result } : task,
              ),
            }
          : result;
      updateRun(id, { updated: { source: run.episode, episode } });
    } finally {
      try {
        await onChanged();
      } finally {
        pending.current.delete(id);
        updateRun(id, { [field]: null });
      }
    }
  };
  const [, redraw] = useReducer((count: number) => count + 1, 0);
  const frontRef = useRef<HTMLDivElement>(null);
  const foldRef = useRef<HTMLButtonElement>(null);
  const focusNext = useRef<"front" | "fold" | null>(null);
  const open = cards.filter(cardIsOpen);
  const settled = cards.filter((card) => !cardIsOpen(card));
  const keys = open.map((card) => card.key);
  const memory = memories.get(stackKey) ?? FRESH;
  const putAway = memory.later.filter((key) => keys.includes(key));
  const order = stackOrder(
    memory.order,
    keys.filter((key) => !putAway.includes(key)),
  );
  const asking = new Set(open.filter((card) => card.kind !== "run").map((card) => card.key));
  const index = stackIndex(memory, order, asking);
  const folded = order.length === 0 && putAway.length > 0;
  const held = Boolean(memory.held) || order.length > 1;
  const shown: StackMemory = { order, index, front: order[index], later: putAway, held };
  useEffect(() => {
    memories.set(stackKey, shown);
  });
  useEffect(() => {
    const target =
      focusNext.current === "fold" ? foldRef.current : frontRef.current?.firstElementChild;
    if (focusNext.current && target instanceof HTMLElement) target.focus();
    focusNext.current = null;
  });
  const remember = (next: StackMemory, focus: "front" | "fold") => {
    memories.set(stackKey, { ...next, front: next.order[next.index] });
    focusNext.current = focus;
    redraw();
  };
  const front = open.find((card) => card.key === order[index]);
  const render = (card: StackCard, onLater?: () => void) =>
    card.kind === "plan" ? (
      <Fragment key={card.key}>{renderPlanCard?.(card.question)}</Fragment>
    ) : card.kind === "run" ? (
      <EpisodeRunCard
        key={card.key}
        run={card.run}
        onLater={onLater}
        state={runStates[card.run.episode.episode_id] ?? EMPTY_RUN}
        onState={(patch) => updateRun(card.run.episode.episode_id, patch)}
        onOperateTask={(task, action) =>
          perform(card.run, "taskActionId", task.operation_id, () =>
            actions.operateTask(task, action),
          )
        }
        onContinue={(id, turns) =>
          perform(card.run, "busyAction", `continue:${id}`, () => actions.addTurns(id, turns))
        }
        onStop={(id) =>
          perform(card.run, "busyAction", `stop:${id}`, () =>
            actions.stop(id, card.run.episode.tasks),
          )
        }
      />
    ) : card.kind === "question" ? (
      <QuestionCard
        key={card.key}
        question={card.question}
        apiBase={`/api/projects/${encodeURIComponent(card.question.project_id)}`}
        origin={
          <span className="question-origin">
            <a
              className="entity-chip project"
              href={`#/projects/${encodeURIComponent(card.question.project_id)}`}
            >
              <Files size={11} aria-hidden="true" />
              {card.question.project_name}
            </a>
            {card.question.source_label && (
              <span title={card.question.source_label}>{card.question.source_label}</span>
            )}
          </span>
        }
        onResolved={() => {
          focusNext.current = "front";
          onAnswered(card.question);
          onChanged();
        }}
        onLater={onLater}
      />
    ) : card.kind === "proposal" ? (
      <ProposalApprovalCard
        key={card.key}
        view={card.view}
        decided={card.decided}
        showProject={card.showProject}
        onDecided={(status) => {
          focusNext.current = "front";
          onDecided(card.view, status);
        }}
        onChanged={onChanged}
        onLater={onLater}
      />
    ) : (
      <AskedApprovalCard
        key={card.key}
        question={card.question}
        card={card.question.card!}
        projectName={card.projectName}
        onResolved={(result) => {
          focusNext.current = "front";
          if (result) onResolved(card.question, result);
          onChanged();
        }}
        onLater={onLater}
      />
    );
  const later = () => {
    const next = stackLater(shown, keys);
    remember(next, next.order.length === 0 ? "fold" : "front");
  };
  const step = (by: number) =>
    remember({ ...shown, index: (index + by + order.length) % order.length }, "front");
  return (
    <EpisodePlanDraftProvider
      key={stackKey}
      questionIds={open.flatMap((card) =>
        card.kind === "plan" ? [card.question.question_id] : [],
      )}
    >
      {settled.map((card) => render(card))}
      {folded ? (
        <button
          ref={foldRef}
          className="approval-quiet approval-fold"
          type="button"
          aria-expanded={false}
          onClick={() => remember({ ...shown, order: putAway, index: 0, later: [] }, "front")}
        >
          <span>
            {putAway.length} {putAway.length === 1 ? "card" : "cards"} waiting
          </span>
          <ChevronDown size={14} aria-hidden="true" />
        </button>
      ) : (
        front && (
          <div
            className="approval-stack"
            data-stacked={order.length > 1 ? "" : undefined}
            data-held={held ? "" : undefined}
          >
            {held && (
              <div className="approval-stack-nav">
                <button type="button" aria-label="Previous card" onClick={() => step(-1)}>
                  <ChevronLeft size={14} aria-hidden="true" />
                </button>
                <span aria-live="polite">
                  {index + 1} of {order.length}
                </span>
                <button type="button" aria-label="Next card" onClick={() => step(1)}>
                  <ChevronRight size={14} aria-hidden="true" />
                </button>
              </div>
            )}
            <div className="approval-stack-front" ref={frontRef}>
              {render(front, later)}
            </div>
          </div>
        )
      )}
    </EpisodePlanDraftProvider>
  );
}

/** One child episode in a chat's dock: its state, its run controls, and its report once ended. */
function EpisodeRunCard({
  run,
  state,
  onState,
  onOperateTask,
  onContinue,
  onStop,
  onLater,
}: {
  run: DockEpisode;
  state: RunState;
  onState: (patch: Partial<RunState>) => void;
  onOperateTask: ComponentProps<typeof EpisodeControls>["onOperateTask"];
  onContinue: ComponentProps<typeof EpisodeControls>["onContinue"];
  onStop: ComponentProps<typeof EpisodeControls>["onStop"];
  onLater?: () => void;
}) {
  const episode = state.updated?.source === run.episode ? state.updated.episode : run.episode;
  const owner = useId();
  const experimentIndex = useResource(
    owner,
    episode.mode === "experiment_loop" ? experimentIndexProfile(episode.project_id) : null,
  );
  const experimentControl =
    experimentIndex.value?.entries.find((entry) => entry.episode.episode_id === episode.episode_id)
      ?.control ?? null;
  const { busyAction, taskActionId, error } = state;
  const setError = (error: string | null) => onState({ error });
  const projection = episodeProjection(episode);
  const Icon = episode.mode === "auto_research" ? Telescope : FlaskConical;
  return (
    <section
      className="approval-card open episode-run-card"
      tabIndex={-1}
      data-state="pending"
      data-health={projection.health}
      aria-label={run.title}
    >
      <div className="approval-head">
        <span className="approval-icon" aria-hidden="true">
          <Icon size={16} />
        </span>
        <div className="approval-title">
          <a href={run.link}>
            <strong>{run.title}</strong>
          </a>
        </div>
        <span className="question-state">{projection.healthLabel}</span>
        <AskedTime at={episode.ended_at ?? episode.created_at} />
      </div>
      {error && (
        <p className="approval-error" role="alert">
          {error}
        </p>
      )}
      <div className="approval-actions">
        {episode.report && (
          <EpisodeReportLink
            className="approval-see"
            projectId={episode.project_id}
            graphTarget={episode.graph_target}
            episodeId={episode.episode_id}
            href={episodeReportPreviewUrl(episode.project_id, episode.episode_id)}
            onOpenError={setError}
          >
            Report
          </EpisodeReportLink>
        )}
        {onLater && (
          <button className="approval-later" type="button" onClick={onLater}>
            Later
          </button>
        )}
        <EpisodeControls
          episode={episode}
          taskControl={projection.taskControl}
          busyAction={busyAction}
          taskActionId={taskActionId}
          additionalTurns={state.additionalTurns}
          onAdditionalTurnsChange={(additionalTurns) => onState({ additionalTurns })}
          experimentControl={experimentControl}
          onOperateTask={onOperateTask}
          onContinue={onContinue}
          onStop={onStop}
          onError={setError}
        />
      </div>
    </section>
  );
}

type Kept = { changes: number } & (
  | { kind: "proposal"; view: ProposalCardView; status: Decision }
  | { kind: "asked"; question: AskedApprovalCardView }
);

/** Whose cards a dock reads; null reads none. */
export type CardSource = { threadId: string | null } | { projectId: string; chatId: string } | null;

/**
 * The member's open cards for one project thread, for every thread when `threadId`
 * is null, or for one shared or node chat. They reload when the open-card version or the thread
 * revision moves, when the next ended child leaves the dock, and after each tap. A card decided here keeps its settled line
 * through the open-card version change its own tap causes, and drops at a fetch
 * after a later change that no longer lists it. Only the newest fetch's answer counts.
 * A failed read hides the cards and backs off before reading again. An answered
 * question leaves at once and stays gone until a read no longer lists it.
 */
export function useAgentCards(source: CardSource, revision: string | null) {
  const version = useAgentCardsVersion();
  const threadId = source && "threadId" in source ? source.threadId : null;
  const chatProject = source && "chatId" in source ? source.projectId : null;
  const chatId = source && "chatId" in source ? source.chatId : null;
  const scope =
    source === null ? null : chatId ? `chat:${chatProject}:${chatId}` : (threadId ?? "");
  const [loaded, setLoaded] = useState<{
    scope: string | null;
    list: AgentCardList | null;
  } | null>(null);
  const [kept, setKept] = useState<ReadonlyMap<string, Kept>>(() => new Map());
  const [answered, setAnswered] = useState<ReadonlySet<string>>(() => new Set());
  const latest = useRef(0);
  const versionChanges = useRef(0);
  const failures = useRef(0);
  const retry = useRef<number | undefined>(undefined);
  const [retryTick, setRetryTick] = useState(0);
  const refetch = useCallback(async () => {
    window.clearTimeout(retry.current);
    if (scope === null) return;
    const mine = ++latest.current;
    const changes = versionChanges.current;
    const list =
      chatProject && chatId
        ? await readChatCards(chatProject, chatId)
        : await readAgentCards(threadId ?? undefined);
    if (mine !== latest.current) return;
    failures.current = list ? 0 : failures.current + 1;
    if (!list) {
      retry.current = window.setTimeout(
        () => setRetryTick((tick) => tick + 1),
        retryBackoffMs(failures.current),
      );
    }
    setLoaded({ scope, list });
    setKept((known) => {
      const listed = new Set([
        ...(list?.proposal_cards ?? []).map(proposalCardKey),
        ...(list?.asked_cards ?? []).map(askedCardKey),
      ]);
      const next = new Map(
        [...known].filter(([key, entry]) => changes <= entry.changes + 1 || listed.has(key)),
      );
      return next.size === known.size ? known : next;
    });
    if (list) {
      const listed = new Set(
        [...(list.questions ?? []), ...(list.episodes ?? []).flatMap((run) => run.questions)].map(
          (question) => question.question_id,
        ),
      );
      setAnswered((known) => {
        const next = new Set([...known].filter((id) => listed.has(id)));
        return next.size === known.size ? known : next;
      });
    }
  }, [chatId, chatProject, scope, threadId]);
  const shownVersion = useRef(version);
  useEffect(() => {
    versionChanges.current += 1;
    const moved = shownVersion.current !== null && shownVersion.current !== version;
    shownVersion.current = version;
    if (moved) void refetch();
  }, [refetch, version]);
  useEffect(() => setKept(new Map()), [scope]);
  useEffect(() => {
    void refetch();
  }, [refetch, revision, retryTick]);
  useEffect(() => () => window.clearTimeout(retry.current), []);
  const keep = useCallback((key: string, entry: Kept) => {
    setKept((known) => new Map(known).set(key, entry));
  }, []);
  const markDecided = useCallback(
    (view: ProposalCardView, status: Decision) =>
      keep(proposalCardKey(view), {
        kind: "proposal",
        view,
        status,
        changes: versionChanges.current,
      }),
    [keep],
  );
  const list = loaded?.scope === scope ? loaded.list : null;
  const nextExpiry = list?.next_expiry_at ?? null;
  useEffect(() => {
    if (!nextExpiry) return;
    const timer = window.setTimeout(
      () => setRetryTick((tick) => tick + 1),
      Math.max(0, Date.parse(nextExpiry) - Date.now()) + EXPIRY_GRACE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [nextExpiry]);
  const markResolved = (question: AgentQuestion, result: ApprovalCardResult) => {
    const served = list?.asked_cards.find((item) => item.question_id === question.question_id);
    if (!served?.card) return;
    const { outcome, reason, rejected } = result;
    keep(askedCardKey(served), {
      kind: "asked",
      question: {
        ...served,
        state: "answered",
        answer: outcome,
        card: { ...served.card, result: { outcome, reason, rejected } },
      },
      changes: versionChanges.current,
    });
  };
  const entries = list ? [...kept.values()] : [];
  const served = list?.proposal_cards ?? [];
  const servedKeys = new Set(served.map(proposalCardKey));
  const proposals = [
    ...served,
    ...entries.flatMap((entry) =>
      entry.kind === "proposal" &&
      !servedKeys.has(proposalCardKey(entry.view)) &&
      (threadId === null || entry.view.thread_id === threadId)
        ? [entry.view]
        : [],
    ),
  ];
  const plans = (list?.asked_cards ?? []).filter(isPlanCardQuestion);
  const servedAsked = (list?.asked_cards ?? []).filter((question) => !isPlanCardQuestion(question));
  const askedKeys = new Set(servedAsked.map(askedCardKey));
  const keptAsked = (key: string) => {
    const entry = kept.get(key);
    return entry?.kind === "asked" ? entry.question : undefined;
  };
  const asked: AskedApprovalCardView[] = [
    ...servedAsked.map((question) => keptAsked(askedCardKey(question)) ?? question),
    ...entries.flatMap((entry) =>
      entry.kind === "asked" && !askedKeys.has(askedCardKey(entry.question))
        ? [entry.question]
        : [],
    ),
  ];
  const questions = (list?.questions ?? []).filter(
    (question) => !answered.has(question.question_id),
  );
  const episodes = (list?.episodes ?? []).map((run) => ({
    ...run,
    questions: run.questions.filter((question) => !answered.has(question.question_id)),
  }));
  const markAnswered = useCallback((question: RelayedQuestion) => {
    setAnswered((known) => new Set(known).add(question.question_id));
  }, []);
  const decisionOf = (view: ProposalCardView): Decision | null => {
    const entry = kept.get(proposalCardKey(view));
    return entry?.kind === "proposal" ? entry.status : null;
  };
  return {
    proposals,
    asked,
    plans,
    episodes,
    questions,
    loading: scope !== null && loaded?.scope !== scope,
    unavailable: loaded?.scope === scope && loaded.list === null,
    decisionOf,
    markDecided,
    markResolved,
    markAnswered,
    refetch,
  };
}
