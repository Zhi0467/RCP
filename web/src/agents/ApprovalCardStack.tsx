import { ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import type {
  AgentCardList,
  AgentQuestion,
  AskedApprovalCardView,
  ProposalCardView,
} from "../core/types";
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
  | { kind: "asked"; key: string; question: AgentQuestion; projectName: string | null };

/** Where a stack stands: its card order, the front card, and whether Later folded it. */
export type StackMemory = { order: string[]; index: number; folded: boolean };

export function proposalCardKey(view: ProposalCardView): string {
  return `proposal:${view.project_id}:${view.proposal.id}`;
}

export function askedCardKey(question: AgentQuestion): string {
  return `asked:${question.question_id}`;
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
  if (order.length <= 1) return { order, index: 0, folded: true };
  const index = Math.min(memory.index, order.length - 1);
  const next = [...order.slice(0, index), ...order.slice(index + 1), order[index]];
  return { order: next, index: index >= next.length - 1 ? 0 : index, folded: false };
}

/** Each stack's place, per stack key, for this page's life; a reload restores server order. */
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
  onChanged,
}: {
  stackKey: string;
  cards: StackCard[];
  onDecided: (view: ProposalCardView, status: Decision) => void;
  onChanged: () => void;
}) {
  const [, redraw] = useReducer((count: number) => count + 1, 0);
  const open = cards.filter(cardIsOpen);
  const settled = cards.filter((card) => !cardIsOpen(card));
  const keys = open.map((card) => card.key);
  const memory = memories.get(stackKey) ?? FRESH;
  const order = stackOrder(memory.order, keys);
  const index = Math.min(memory.index, Math.max(order.length - 1, 0));
  const folded = memory.folded && order.length > 0;
  const shown: StackMemory = { order, index, folded };
  useEffect(() => {
    memories.set(stackKey, { order, index, folded });
  });
  const remember = (next: StackMemory) => {
    memories.set(stackKey, next);
    redraw();
  };
  const front = open.find((card) => card.key === order[index]);
  const render = (card: StackCard, onLater?: () => void) =>
    card.kind === "proposal" ? (
      <ProposalApprovalCard
        key={card.key}
        view={card.view}
        decided={card.decided}
        showProject={card.showProject}
        onDecided={(status) => onDecided(card.view, status)}
        onChanged={onChanged}
        onLater={onLater}
      />
    ) : (
      <AskedApprovalCard
        key={card.key}
        question={card.question}
        card={card.question.card!}
        projectName={card.projectName}
        onResolved={onChanged}
        onLater={onLater}
      />
    );
  const step = (by: number) =>
    remember({ ...shown, index: (index + by + order.length) % order.length });
  return (
    <>
      {settled.map((card) => render(card))}
      {front &&
        (folded ? (
          <button
            className="approval-quiet approval-fold"
            type="button"
            aria-expanded={false}
            onClick={() => remember({ ...shown, folded: false })}
          >
            <span>
              {order.length} {order.length === 1 ? "card" : "cards"} waiting
            </span>
            <ChevronDown size={14} aria-hidden="true" />
          </button>
        ) : (
          <div className="approval-stack" data-stacked={order.length > 1 ? "" : undefined}>
            {order.length > 1 && (
              <div className="approval-stack-nav">
                <button type="button" aria-label="Previous card" onClick={() => step(-1)}>
                  <ChevronLeft size={14} aria-hidden="true" />
                </button>
                <span>
                  {index + 1} of {order.length}
                </span>
                <button type="button" aria-label="Next card" onClick={() => step(1)}>
                  <ChevronRight size={14} aria-hidden="true" />
                </button>
              </div>
            )}
            <div className="approval-stack-front">
              {render(front, () => remember(stackLater(shown, keys)))}
            </div>
          </div>
        ))}
    </>
  );
}

type Decided = { view: ProposalCardView; status: Decision; fetchesLeft: number };

/**
 * The member's open cards for one project thread, or for every thread when
 * `threadId` is null. They reload when the open-card version or the thread
 * revision moves, and after each tap. A decided Proposal card shows its outcome
 * through the refetch its tap starts and drops at the one after.
 */
export function useAgentCards(threadId: string | null, revision: string | null) {
  const version = useAgentCardsVersion();
  const scope = threadId ?? "";
  const [loaded, setLoaded] = useState<{ scope: string; list: AgentCardList | null } | null>(null);
  const [decided, setDecided] = useState<ReadonlyMap<string, Decided>>(() => new Map());
  const latest = useRef(0);
  const refetch = useCallback(async () => {
    const mine = ++latest.current;
    const list = await readAgentCards(scope || undefined);
    if (mine !== latest.current) return;
    setLoaded({ scope, list });
    setDecided((known) => {
      if (!known.size) return known;
      const next = new Map<string, Decided>();
      for (const [key, entry] of known)
        if (entry.fetchesLeft > 1) next.set(key, { ...entry, fetchesLeft: entry.fetchesLeft - 1 });
      return next;
    });
  }, [scope]);
  useEffect(() => {
    void refetch();
  }, [refetch, version, revision]);
  const markDecided = useCallback((view: ProposalCardView, status: Decision) => {
    setDecided((known) =>
      new Map(known).set(proposalCardKey(view), { view, status, fetchesLeft: 2 }),
    );
  }, []);
  const list = loaded?.scope === scope ? loaded.list : null;
  const served = list?.proposal_cards ?? [];
  const servedKeys = new Set(served.map(proposalCardKey));
  const proposals = [
    ...served,
    ...[...decided.values()]
      .map((entry) => entry.view)
      .filter(
        (view) =>
          !servedKeys.has(proposalCardKey(view)) &&
          (threadId === null || view.thread_id === threadId),
      ),
  ];
  const asked: AskedApprovalCardView[] = list?.asked_cards ?? [];
  const decisionOf = (view: ProposalCardView): Decision | null =>
    decided.get(proposalCardKey(view))?.status ?? null;
  return { proposals, asked, decisionOf, markDecided, refetch };
}
