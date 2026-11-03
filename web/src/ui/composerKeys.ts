import { CHAT_ARROW_SCROLL_STEP_PX } from "../core/uiConstants.ts";

/** Whether Return sends a composer's message. On a touch phone it types a newline,
 * so a message can span lines and only the send button sends. */
export function returnSends(event: { key: string; shiftKey: boolean }): boolean {
  return (
    event.key === "Enter" && !event.shiftKey && !window.matchMedia("(pointer: coarse)").matches
  );
}

type ComposerArrowEvent = {
  key: string;
  altKey: boolean;
  ctrlKey: boolean;
  metaKey: boolean;
  shiftKey: boolean;
  defaultPrevented: boolean;
  nativeEvent: { isComposing: boolean };
  currentTarget: { value: string };
  preventDefault(): void;
};

/** Up or Down in an empty composer scrolls the transcript; with text they move the caret. */
export function arrowScrollsTranscript(
  event: ComposerArrowEvent,
  transcript: { scrollBy(options: ScrollToOptions): void } | null,
): boolean {
  const direction = event.key === "ArrowUp" ? -1 : event.key === "ArrowDown" ? 1 : 0;
  if (
    !direction ||
    !transcript ||
    event.currentTarget.value ||
    event.defaultPrevented ||
    event.nativeEvent.isComposing ||
    event.altKey ||
    event.ctrlKey ||
    event.metaKey ||
    event.shiftKey
  )
    return false;
  event.preventDefault();
  transcript.scrollBy({ top: direction * CHAT_ARROW_SCROLL_STEP_PX });
  return true;
}
