import { useEffect, useRef, type RefObject } from "react";
import { chatFollowsBottom, chatReaderMovedUp } from "../chat/chatLayout";
import { CHAT_SCROLL_BOTTOM_TOLERANCE_PX } from "../core/uiConstants";
// Where the reader left each chat in this session, kept only while they had
// scrolled up; a chat without an entry opens at its bottom.
const chatScrollPositions = new Map<string, number>();

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
  useEffect(() => {
    if (lastChatIdRef.current !== scrollKey) {
      lastChatIdRef.current = scrollKey;
      restoreTopRef.current = chatScrollPositions.get(scrollKey) ?? null;
      shouldStickToBottomRef.current = restoreTopRef.current === null;
    }
    const element = chatLinesRef.current;
    if (!element) return;
    if (restoreTopRef.current !== null) {
      element.scrollTop = restoreTopRef.current;
      lastScrollTopRef.current = element.scrollTop;
      if (Math.abs(element.scrollTop - restoreTopRef.current) < 1) restoreTopRef.current = null;
      return;
    }
    // A refresh can render between the reader's scroll and its scroll event; their
    // position wins over a follow decided before they moved.
    if (chatReaderMovedUp(chatScrollObservation(element, lastScrollTopRef.current)))
      shouldStickToBottomRef.current = false;
    if (!shouldStickToBottomRef.current) return;
    element.scrollTop = element.scrollHeight;
    lastScrollTopRef.current = element.scrollTop;
    // Question cards load after the transcript and end it, so they move the bottom too.
  }, [scrollKey, chatLinesRef, transcript, questions]);
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
    if (shouldStickToBottomRef.current) chatScrollPositions.delete(scrollKey);
    else chatScrollPositions.set(scrollKey, element.scrollTop);
  };

  return { shouldStickToBottomRef, restoreTopRef, handleChatScroll };
}
