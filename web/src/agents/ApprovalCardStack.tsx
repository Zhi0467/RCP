import { ChevronDown, ChevronLeft, ChevronRight, Files } from "lucide-react";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type {
  AgentCardList,
  AgentQuestion,
  ApprovalCardResult,
  AskedApprovalCardView,
  ProposalCardView,
  RelayedQuestion,
} from "../core/types";
import { QuestionCard } from "../chat/QuestionCard";
import { retryBackoffMs } from "../core/resourceStore";
import { AskedApprovalCard, ProposalApprovalCard } from "./ApprovalCard";
import { readAgentCards } from "./agentThreadsApi";
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
  | { kind: "question"; key: string; question: RelayedQuestion };

/**
 * Where a stack stands: its card order, the front card, whether Later folded it,
 * and whether it ever held more than one card, so its space stays put as it empties.
 */
export type StackMemory = { order: string[]; index: number; folded: boolean; held?: boolean };

export function proposalCardKey(view: ProposalCardView): string {
  return `proposal:${view.project_id}:${view.proposal.id}`;
}

export function askedCardKey(question: AgentQuestion): string {
  return `asked:${question.question_id}`;
}

export function questionCardKey(question: RelayedQuestion): string {
  return `question:${question.question_id}`;
}

/** Surviving cards keep their order, new cards join at the back, settled ones drop. */
export function stackOrder(previous: readonly string[], keys: readonly string[]): string[] {
  const live = new Set(keys);
  const kept = previous.filter((key) => live.has(key));
  const known = new Set(kept);
  return [...kept, ...keys.filter((key) => !known.has(key))];
}

/** Later sends the front card to the back; on the last card it folds the stack. */
export function stackLater(memory: StackMemory, keys: readonly string[]): StackMemory {
  const order = stackOrder(memory.order, keys);
  if (order.length <= 1) return { ...memory, order, index: 0, folded: true };
  const index = Math.min(memory.index, order.length - 1);
  const next = [...order.slice(0, index), ...order.slice(index + 1), order[index]];
  return { ...memory, order: next, index: index >= next.length - 1 ? 0 : index, folded: false };
}

/** Each rendered stack's place, per stack key; a reload restores server order. */
const memories = new Map<string, StackMemory>();
const FRESH: StackMemory = { order: [], index: 0, folded: false };

function cardIsOpen(card: StackCard): boolean {
  if (card.kind === "proposal") return card.decided === null;
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
}: {
  stackKey: string;
  cards: StackCard[];
  onDecided: (view: ProposalCardView, status: Decision) => void;
  onResolved: (question: AgentQuestion, result: ApprovalCardResult) => void;
  onAnswered: (question: RelayedQuestion) => void;
  onChanged: () => void;
}) {
  const [, redraw] = useReducer((count: number) => count + 1, 0);
  const frontRef = useRef<HTMLDivElement>(null);
  const foldRef = useRef<HTMLButtonElement>(null);
  const focusNext = useRef<"front" | "fold" | null>(null);
  const open = cards.filter(cardIsOpen);
  const settled = cards.filter((card) => !cardIsOpen(card));
  const keys = open.map((card) => card.key);
  const memory = memories.get(stackKey) ?? FRESH;
  const order = stackOrder(memory.order, keys);
  const index = Math.min(memory.index, Math.max(order.length - 1, 0));
  const folded = memory.folded && order.length > 0;
  const held = Boolean(memory.held) || order.length > 1;
  const shown: StackMemory = { order, index, folded, held };
  useEffect(() => {
    memories.set(stackKey, { order, index, folded, held });
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
    card.kind === "question" ? (
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
    remember(next, next.folded ? "fold" : "front");
  };
  const step = (by: number) =>
    remember({ ...shown, index: (index + by + order.length) % order.length }, "front");
  return (
    <>
      {settled.map((card) => render(card))}
      {front &&
        (folded ? (
          <button
            ref={foldRef}
            className="approval-quiet approval-fold"
            type="button"
            aria-expanded={false}
            onClick={() => remember({ ...shown, folded: false }, "front")}
          >
            <span>
              {order.length} {order.length === 1 ? "card" : "cards"} waiting
            </span>
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        ) : (
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
        ))}
    </>
  );
}

type Kept = { changes: number } & (
  | { kind: "proposal"; view: ProposalCardView; status: Decision }
  | { kind: "asked"; question: AskedApprovalCardView }
);

/**
 * The member's open cards for one project thread, or for every thread when
 * `threadId` is null. They reload when the open-card version or the thread
 * revision moves, and after each tap. A card decided here keeps its settled line
 * through the open-card version change its own tap causes, and drops at a fetch
 * after a later change that no longer lists it. Only the newest fetch's answer counts.
 * A failed read hides the cards and backs off before reading again. An answered
 * question leaves at once and stays gone until a read no longer lists it.
 */
export function useAgentCards(threadId: string | null, revision: string | null) {
  const version = useAgentCardsVersion();
  const scope = threadId ?? "";
  const [loaded, setLoaded] = useState<{ scope: string; list: AgentCardList | null } | null>(null);
  const [kept, setKept] = useState<ReadonlyMap<string, Kept>>(() => new Map());
  const [answered, setAnswered] = useState<ReadonlySet<string>>(() => new Set());
  const latest = useRef(0);
  const versionChanges = useRef(0);
  const failures = useRef(0);
  const retry = useRef<number | undefined>(undefined);
  const [retryTick, setRetryTick] = useState(0);
  const refetch = useCallback(async () => {
    window.clearTimeout(retry.current);
    const mine = ++latest.current;
    const changes = versionChanges.current;
    const list = await readAgentCards(scope || undefined);
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
      const listed = new Set((list.questions ?? []).map((question) => question.question_id));
      setAnswered((known) => {
        const next = new Set([...known].filter((id) => listed.has(id)));
        return next.size === known.size ? known : next;
      });
    }
  }, [scope]);
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
  const servedAsked = list?.asked_cards ?? [];
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
    questions,
    decisionOf,
    markDecided,
    markResolved,
    markAnswered,
    refetch,
  };
}
