export const CHAT_LIST_DEFAULT_WIDTH = 260;
export const CHAT_LIST_COLLAPSED_WIDTH = 0;
export const CHAT_LIST_DIVIDER_WIDTH = 1;
export const CHAT_LIST_MIN_WIDTH = 190;
export const CHAT_LIST_MIN_WIDTH_COMPACT = 110;
export const CHAT_LIST_MAX_WIDTH = 420;
export const CHAT_SURFACE_MIN_WIDTH = 300;

export interface ChatListWidthBounds {
  minimum: number;
  maximum: number;
}

export function chatListWidthBounds(containerWidth: number): ChatListWidthBounds {
  const minimum = containerWidth < 720 ? CHAT_LIST_MIN_WIDTH_COMPACT : CHAT_LIST_MIN_WIDTH;
  const maximum = Math.max(
    minimum,
    Math.min(CHAT_LIST_MAX_WIDTH, containerWidth - CHAT_SURFACE_MIN_WIDTH),
  );
  return { minimum, maximum };
}

export function isChatListToggleShortcut(
  event: Pick<KeyboardEvent, "key" | "metaKey" | "altKey" | "ctrlKey" | "shiftKey" | "repeat">,
): boolean {
  return (
    event.metaKey &&
    !event.altKey &&
    !event.ctrlKey &&
    !event.shiftKey &&
    !event.repeat &&
    event.key.toLowerCase() === "b"
  );
}

/** Where a scrolled chat view stands, from the last position it observed. */
export interface ChatScrollObservation {
  previousTop: number;
  top: number;
  scrollHeight: number;
  clientHeight: number;
}

/**
 * True when the reader moved the view up. A position that dropped only because
 * content below shrank lands on the bottom, so it is not a move up.
 */
export function chatReaderMovedUp(observation: ChatScrollObservation): boolean {
  const distance = observation.scrollHeight - observation.top - observation.clientHeight;
  return observation.top < observation.previousTop - 1 && distance > 1;
}

/** A chat follows new content only while the reader sits at its bottom. */
export function chatFollowsBottom(observation: ChatScrollObservation, tolerance: number): boolean {
  if (chatReaderMovedUp(observation)) return false;
  return observation.scrollHeight - observation.top - observation.clientHeight <= tolerance;
}
