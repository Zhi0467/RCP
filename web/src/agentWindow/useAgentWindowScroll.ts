import { useEffect, useRef, useState, useSyncExternalStore, type RefObject } from "react";
import { chatFarFromBottom, chatFollowsBottom, chatReaderMovedUp } from "../chat/chatLayout";
import { flashRect, toRect } from "../comments/react/domText";
import { hashWithoutTurn, turnFromHash } from "../core/graphTarget";
import { CHAT_SCROLL_BOTTOM_TOLERANCE_PX } from "../core/uiConstants";
// Where the reader left each chat in this session, kept only while they had
// scrolled up; a chat without an entry opens at its bottom.
const chatScrollPositions = new Map<string, number>();
const TURN_FLASH_DELAY_MS = 350;

function subscribeHash(listener: () => void): () => void {
  window.addEventListener("hashchange", listener);
  return () => window.removeEventListener("hashchange", listener);
}

function chatScrollObservation(element: HTMLElement, previousTop: number) {
  return {
    previousTop,
    top: element.scrollTop,
    scrollHeight: element.scrollHeight,
    clientHeight: element.clientHeight,
  };
}

export function useAgentWindowScroll(
  scrollKey: string,
  chatLinesRef: RefObject<HTMLDivElement | null>,
  transcript: unknown,
  questions: unknown,
) {
  // A saved position is applied as the transcript renders, until the view
  // reaches it or the reader scrolls.
  const restoreTopRef = useRef(chatScrollPositions.get(scrollKey) ?? null);
  const shouldStickToBottomRef = useRef(restoreTopRef.current === null);
  // The last scroll position this view observed; a smaller one means the reader
  // moved up, and only the reader's own return to the bottom re-sticks.
  const lastScrollTopRef = useRef(0);
  const lastChatIdRef = useRef(scrollKey);
  const [farFromBottom, setFarFromBottom] = useState(false);
  const requestedTurn = useSyncExternalStore(
    subscribeHash,
    () => turnFromHash(window.location.hash),
    () => null,
  );
  const pendingTurnRef = useRef<string | null>(null);
  const flashTimerRef = useRef<number | undefined>(undefined);
  useEffect(() => {
    if (!requestedTurn) return;
    pendingTurnRef.current = requestedTurn;
    restoreTopRef.current = null;
    shouldStickToBottomRef.current = false;
  }, [requestedTurn]);
  useEffect(() => () => window.clearTimeout(flashTimerRef.current), []);
  useEffect(() => {
    if (lastChatIdRef.current !== scrollKey) {
      lastChatIdRef.current = scrollKey;
      restoreTopRef.current = pendingTurnRef.current
        ? null
        : (chatScrollPositions.get(scrollKey) ?? null);
      shouldStickToBottomRef.current = restoreTopRef.current === null && !pendingTurnRef.current;
    }
    const element = chatLinesRef.current;
    if (!element) return;
    const settle = () => setFarFromBottom(chatFarFromBottom(chatScrollObservation(element, 0)));
    const turn = pendingTurnRef.current;
    const reply = turn
      ? [
          ...element.querySelectorAll(`.agent-chat-view-line[data-task-id="${CSS.escape(turn)}"]`),
        ].at(-1)
      : undefined;
    if (reply) {
      pendingTurnRef.current = null;
      window.history.replaceState(window.history.state, "", hashWithoutTurn(window.location.hash));
      reply.scrollIntoView({ behavior: "smooth", block: "center" });
      window.clearTimeout(flashTimerRef.current);
      flashTimerRef.current = window.setTimeout(
        () => flashRect(toRect(reply.getBoundingClientRect())),
        TURN_FLASH_DELAY_MS,
      );
    }
    if (restoreTopRef.current !== null) {
      element.scrollTop = restoreTopRef.current;
      lastScrollTopRef.current = element.scrollTop;
      if (Math.abs(element.scrollTop - restoreTopRef.current) < 1) restoreTopRef.current = null;
      settle();
      return;
    }
    // A refresh can render between the reader's scroll and its scroll event; their
    // position wins over a follow decided before they moved.
    if (chatReaderMovedUp(chatScrollObservation(element, lastScrollTopRef.current)))
      shouldStickToBottomRef.current = false;
    if (shouldStickToBottomRef.current) {
      element.scrollTop = element.scrollHeight;
      lastScrollTopRef.current = element.scrollTop;
    }
    settle();
    // Question cards load after the transcript and end it, so they move the bottom too.
  }, [scrollKey, chatLinesRef, transcript, questions, requestedTurn]);
  const handleChatScroll = (event: React.UIEvent<HTMLDivElement>) => {
    const element = event.currentTarget;
    // A restore the transcript was still too short to reach is not the reader's move.
    if (restoreTopRef.current !== null) {
      if (element.scrollTop === lastScrollTopRef.current) return;
      restoreTopRef.current = null;
    }
    shouldStickToBottomRef.current = chatFollowsBottom(
      chatScrollObservation(element, lastScrollTopRef.current),
      CHAT_SCROLL_BOTTOM_TOLERANCE_PX,
    );
    lastScrollTopRef.current = element.scrollTop;
    setFarFromBottom(chatFarFromBottom(chatScrollObservation(element, 0)));
    if (shouldStickToBottomRef.current) chatScrollPositions.delete(scrollKey);
    else chatScrollPositions.set(scrollKey, element.scrollTop);
  };

  const follow = () => {
    shouldStickToBottomRef.current = true;
    restoreTopRef.current = null;
  };

  const jumpToLatest = () => {
    const element = chatLinesRef.current;
    if (!element) return;
    const instant = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    element.scrollTo({ top: element.scrollHeight, behavior: instant ? "auto" : "smooth" });
  };

  return {
    shouldStickToBottomRef,
    restoreTopRef,
    handleChatScroll,
    follow,
    farFromBottom,
    jumpToLatest,
  };
}

export type AgentWindowScroll = ReturnType<typeof useAgentWindowScroll>;
