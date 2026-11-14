import { CommentRequestError } from "../api.ts";
import type { CommentTarget, CommentThread } from "../types.ts";

/** Only threads on the asked targets: a read that answers for another target never shows. */
export function targetThreads(
  threads: readonly CommentThread[],
  kind: CommentTarget["kind"],
  ids: readonly string[],
): CommentThread[] {
  const wanted = new Set(ids);
  return threads.filter((thread) => thread.target_kind === kind && wanted.has(thread.target_id));
}

/** One client key per set of threads until that send succeeds, so a retry after an
 *  unanswered request repeats the same admission instead of sending twice. */
export function sendKeys(newKey: () => string = () => crypto.randomUUID()) {
  let last: { ids: string; key: string } | null = null;
  return {
    key(threadIds: readonly string[]) {
      const ids = JSON.stringify([...threadIds].sort());
      if (last?.ids !== ids) last = { ids, key: newKey() };
      return last.key;
    },
    done() {
      last = null;
    },
  };
}

export function errorText(cause: unknown): string {
  if (cause instanceof CommentRequestError) return cause.message;
  return cause instanceof Error ? cause.message : String(cause);
}

/** Open threads first in the order they were drawn, numbered as their pins are. */
export function pinNumbers(threads: readonly CommentThread[]): Map<string, number> {
  return new Map(
    threads
      .filter((thread) => thread.state === "open")
      .map((thread, index) => [thread.thread_id, index + 1]),
  );
}

/** The text around a quote, which re-finds it among repeats after edits. */
export function surroundingText(text: string, start: number, end: number, pad = 160): string {
  return text.slice(Math.max(0, start - pad), Math.min(text.length, end + pad));
}

/** The quote as a text anchor keeps it: trimmed and within the anchor's bound. */
export const MAX_QUOTE_CHARS = 4000;
