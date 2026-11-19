import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  FlaskConical,
  Files,
  Telescope,
} from "lucide-react";
import {
  Fragment,
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type ReactNode,
} from "react";
import type {
  AgentCardList,
  AgentQuestion,
  ApprovalCardResult,
  AskedApprovalCardView,
  DockEpisode,
  ProposalCardView,
  RelayedQuestion,
} from "../core/types";
import { AskedTime, QuestionCard } from "../chat/QuestionCard";
import { retryBackoffMs } from "../core/resourceStore";
import { episodeProjection, episodeReportPreviewUrl } from "../experiments/campaigns";
import { EpisodeControls } from "../experiments/EpisodeControls";
import { EpisodeReportLink } from "../experiments/EpisodeReportLink";
import { useEpisodeActions } from "../experiments/episodeActions";
import { AskedApprovalCard, ProposalApprovalCard } from "./ApprovalCard";
import { readAgentCards, readChatCards } from "./agentThreadsApi";
import { isPlanCardQuestion } from "./experimentPlanModel";
import { useAgentCardsVersion } from "./useAgentThreads";

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
 * Where a stack stands: its shown card order, the front card, the cards Later put
 * away, and whether it ever held more than one card, so its space stays put as it empties.
 */
export type StackMemory = { order: string[]; index: number; later: string[]; held?: boolean };

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
 * A chat's dock cards after its Proposal and asked cards: pending plan cards, each
 * child episode's open questions followed by the run itself, then other relayed questions.
 */
export function dockStackCards(
  plans: readonly AgentQuestion[],
  episodes: readonly DockEpisode[],
  questions: readonly RelayedQuestion[],
): StackCard[] {
  return [
    ...plans.map((question): StackCard => ({ kind: "plan", key: planCardKey(question), question })),
    ...episodes.flatMap((run): StackCard[] => [
      ...run.questions.map((question): StackCard => ({
        kind: "question",
        key: questionCardKey(question),
        question,
      })),
      { kind: "run", key: runCardKey(run), run },
    ]),
    ...questions.map((question): StackCard => ({
      kind: "question",
      key: questionCardKey(question),
      question,
    })),
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
  onChanged: () => void;
  /** Draws a plan card; the view that owns the stack supplies it. */
  renderPlanCard?: (question: AgentQuestion) => ReactNode;
}) {
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
  const index = Math.min(memory.index, Math.max(order.length - 1, 0));
  const folded = order.length === 0 && putAway.length > 0;
  const held = Boolean(memory.held) || order.length > 1;
  const shown: StackMemory = { order, index, later: putAway, held };
  useEffect(() => {
    memories.set(stackKey, shown);
  });
  useEffect(() => () => void memories.delete(stackKey), [stackKey]);
  useEffect(() => {
    const target =
      focusNext.current === "fold" ? foldRef.current : frontRef.current?.firstElementChild;
    if (focusNext.current && target instanceof HTMLElement) target.focus();
    focusNext.current = null;
  });
  const remember = (next: StackMemory, focus: "front" | "fold") => {
    memories.set(stackKey, next);
    focusNext.current = focus;
    redraw();
  };
  const front = open.find((card) => card.key === order[index]);
  const render = (card: StackCard, onLater?: () => void) =>
    card.kind === "plan" ? (
      <Fragment key={card.key}>{renderPlanCard?.(card.question)}</Fragment>
    ) : card.kind === "run" ? (
      <EpisodeRunCard key={card.key} run={card.run} onChanged={onChanged} onLater={onLater} />
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
    <>
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
    </>
  );
}

/** One child episode in a chat's dock: its state, its run controls, and its report once ended. */
function EpisodeRunCard({
  run,
  onChanged,
  onLater,
}: {
  run: DockEpisode;
  onChanged: () => void;
  onLater?: () => void;
}) {
  const { episode } = run;
  const actions = useEpisodeActions(episode.project_id);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [taskActionId, setTaskActionId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const projection = episodeProjection(episode);
  const Icon = episode.mode === "auto_research" ? Telescope : FlaskConical;
  const busy = async (
    set: (key: string | null) => void,
    key: string,
    act: () => Promise<unknown>,
  ) => {
    set(key);
    try {
      await act();
    } finally {
      set(null);
      onChanged();
    }
  };
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
          onOperateTask={(task, action) =>
            busy(setTaskActionId, task.operation_id, () => actions.operateTask(task, action))
          }
          onContinue={(episodeId, turns) =>
            busy(setBusyAction, `continue:${episodeId}`, () => actions.addTurns(episodeId, turns))
          }
          onStop={(episodeId) =>
            busy(setBusyAction, `stop:${episodeId}`, () => actions.stop(episodeId, episode.tasks))
          }
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
 * revision moves, and after each tap. A card decided here keeps its settled line
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
