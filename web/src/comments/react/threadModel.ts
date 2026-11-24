import { ApiError } from "../../core/api.ts";
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

/** One client key per set of threads until that send succeeds or the server refuses
 *  it, so a retry after an unanswered request or a server fault repeats the same
 *  admission instead of sending twice. */
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
    failed(cause: unknown) {
      if (cause instanceof ApiError && cause.status < 500) last = null;
    },
  };
}

export function errorText(cause: unknown): string {
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

/** One Send: the oldest sendable threads, at most `limit`. The server's length
 *  refusal lowers `limit` until the batch fits. */
export function sendBatch(
  threads: readonly CommentThread[],
  limit: number,
): { batch: CommentThread[]; sendable: number } {
  const sendable = threads
    .filter((thread) => thread.state === "open" && thread.can_send)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  return { batch: sendable.slice(0, limit), sendable: sendable.length };
}
