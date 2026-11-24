import { ApiError } from "../../core/api.ts";
import type { CommentAnchor, CommentTarget, CommentThread } from "../types.ts";

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

const NAME_MAX_CHARS = 64;

function anchorLines(anchor: CommentAnchor, drawnVersion: string | null): string[] {
  const box = (rect: { x: number; y: number; width: number; height: number }) =>
    `x=${rect.x}, y=${rect.y}, width=${rect.width}, height=${rect.height}`;
  const lines: string[] = [];
  if (anchor.where) lines.push(`Location: ${anchor.where}`);
  if (drawnVersion) lines.push(`Drawn version: ${drawnVersion}`);
  if (anchor.kind === "whole") lines.push("Whole target");
  else if (anchor.kind === "text") {
    lines.push(`Quote: ${anchor.text}`);
    if (anchor.surrounding_text) lines.push(`Context: ${anchor.surrounding_text}`);
  } else if (anchor.kind === "element") {
    lines.push(`Element: ${anchor.path}`);
    if (anchor.label) lines.push(`Label: ${anchor.label}`);
    if (anchor.text) lines.push(`Quote: ${anchor.text}`);
  } else if (anchor.kind === "pdf") {
    lines.push(`PDF page: ${anchor.page}`);
    if (anchor.rect) lines.push(`Box: ${box(anchor.rect)}`);
    if (anchor.text) lines.push(`Quote: ${anchor.text}`);
  } else {
    lines.push(`Box: ${box(anchor.rect)}`);
    lines.push(`Viewport: ${anchor.viewport.width} x ${anchor.viewport.height}`);
    for (const element of anchor.elements ?? []) {
      lines.push(`Element: ${element.path} ${element.label}`.trimEnd());
      if (element.region) lines.push(`Element region: ${box(element.region)}`);
      if (element.text) lines.push(`Quote: ${element.text}`);
    }
    if (anchor.labels) lines.push(`Nearby text: ${anchor.labels}`);
  }
  return lines;
}

/** About how long the server renders a thread into a turn (`render_thread`); the
 *  server's own length refusal stays the backstop. */
export function renderedThreadChars(thread: CommentThread): number {
  const posts = thread.posts.map((post) => {
    const names = new Map(
      (post.mentions ?? []).map((item) => [item.id, item.display_name.slice(0, NAME_MAX_CHARS)]),
    );
    const text = post.text.replace(/<@member:([^<>\s]+)>/g, (token, id: string) =>
      names.has(id) ? `@${names.get(id)}` : token,
    );
    return `${post.author.display_name.slice(0, NAME_MAX_CHARS)}: ${text}`;
  });
  return [...anchorLines(thread.anchor, thread.drawn_version), ...posts].join("\n").length;
}

/** One Send: the oldest sendable threads that fit `maxSelections` and `maxChars`,
 *  at most `cap` of them; always at least one, so the server names a lone misfit. */
export function sendBatch(
  threads: readonly CommentThread[],
  {
    maxSelections,
    maxChars,
    cap = Infinity,
  }: { maxSelections: number; maxChars: number; cap?: number },
): { batch: CommentThread[]; sendable: number } {
  const sendable = threads
    .filter((thread) => thread.state === "open" && thread.can_send)
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const batch: CommentThread[] = [];
  let chars = 0;
  for (const thread of sendable) {
    if (batch.length >= Math.min(maxSelections, Math.max(1, cap))) break;
    const next = chars + (batch.length ? 2 : 0) + renderedThreadChars(thread);
    if (batch.length && next > maxChars) break;
    batch.push(thread);
    chars = next;
  }
  return { batch, sendable: sendable.length };
}
