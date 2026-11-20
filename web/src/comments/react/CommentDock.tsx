import { useCallback, useEffect, useId, useState, useSyncExternalStore } from "react";
import { MessageCircle, X } from "lucide-react";
import { anchorQuote } from "../model.ts";
import type { CommentThread } from "../types.ts";

/** A thread waiting at a composer: what its chip and review row show. */
export interface DockedThread {
  thread_id: string;
  quote: string;
  text: string;
}

/** Where a comment surface puts a saved thread so the next message carries it. */
export type DockThreads = (threads: readonly CommentThread[]) => void;

export function dockedThread(thread: CommentThread): DockedThread {
  const last = thread.posts[thread.posts.length - 1];
  return {
    thread_id: thread.thread_id,
    quote: anchorQuote(thread.anchor) || thread.anchor.where || "",
    text: last?.text ?? "",
  };
}

const docks = new Map<string, readonly DockedThread[]>();
const listeners = new Set<() => void>();
const NONE: readonly DockedThread[] = [];

function writeDock(scope: string, items: readonly DockedThread[]) {
  if (items.length) docks.set(scope, items);
  else docks.delete(scope);
  listeners.forEach((listener) => listener());
}

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The dock scope of one chat, which any surface may dock into while it is not shown. */
export function chatDockScope(projectId: string, chatId: string) {
  return `${projectId}:${chatId}`;
}

/** Docks threads at a composer, replacing a thread already there by id. */
export function dockInScope(scope: string, threads: readonly DockedThread[]) {
  const ids = new Set(threads.map((item) => item.thread_id));
  const kept = (docks.get(scope) ?? NONE).filter((item) => !ids.has(item.thread_id));
  writeDock(scope, [...kept, ...threads]);
}

/** The threads one composer will send with its next message. */
export function useCommentDock(scope: string) {
  const snapshot = () => docks.get(scope) ?? NONE;
  const items = useSyncExternalStore(subscribe, snapshot, snapshot);
  const [open, setOpen] = useState(false);
  useEffect(() => setOpen(false), [scope]);
  const dock = useCallback<DockThreads>(
    (threads) => dockInScope(scope, threads.map(dockedThread)),
    [scope],
  );
  const remove = useCallback(
    (threadId: string) =>
      writeDock(
        scope,
        (docks.get(scope) ?? NONE).filter((item) => item.thread_id !== threadId),
      ),
    [scope],
  );
  const clear = useCallback(
    (threadIds: readonly string[]) =>
      writeDock(
        scope,
        (docks.get(scope) ?? NONE).filter((item) => !threadIds.includes(item.thread_id)),
      ),
    [scope],
  );
  return { items, dock, remove, clear, open, setOpen };
}

export type CommentDock = ReturnType<typeof useCommentDock>;

/** The composer's docked comments: a count that opens the list, each removable. */
export function DockedComments({ dock, busy }: { dock: CommentDock; busy: boolean }) {
  const panelId = useId();
  const { items, open, setOpen } = dock;
  if (!items.length) return null;
  return (
    <>
      <div className="chat-annotation-summary">
        <button
          className={`chat-annotation-count${open ? " is-open" : ""}`}
          type="button"
          aria-expanded={open}
          aria-controls={panelId}
          onClick={() => setOpen((value) => !value)}
        >
          <MessageCircle size={12} /> {items.length} comment{items.length === 1 ? "" : "s"}
        </button>
      </div>
      {open && (
        <section className="chat-annotation-review" id={panelId} aria-label="Docked comments">
          <header>
            <strong>Comments</strong>
            <button
              type="button"
              className="icon-button"
              aria-label="Close comments"
              onClick={() => setOpen(false)}
            >
              <X size={14} />
            </button>
          </header>
          <div className="chat-annotation-review-list">
            {items.map((item) => (
              <article key={item.thread_id} className="docked-comment">
                {item.quote && <blockquote>{item.quote}</blockquote>}
                <p>{item.text}</p>
                <button
                  type="button"
                  aria-label="Remove comment from message"
                  disabled={busy}
                  onClick={() => dock.remove(item.thread_id)}
                >
                  <X size={12} /> Remove
                </button>
              </article>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
