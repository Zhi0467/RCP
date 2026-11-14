import { useEffect, useRef, useState, type ReactNode } from "react";
import { X } from "lucide-react";
import { railState } from "../model.ts";
import type { CommentThread } from "../types.ts";
import { CommentThreadCard } from "./CommentThreadCard";
import type { CommentsController } from "./useComments";
import "../comments.css";
import "./rail.css";

export interface CommentListProps {
  comments: CommentsController;
  agentLabel: string;
  wholeLabel: string;
  selected: string | null;
  onLocate: (thread: CommentThread) => void;
  /** Why a thread has no pin here, shown on hover; undefined when it has one. */
  note?: (thread: CommentThread) => string | undefined;
}

/** The open threads, the Resolved fold, and Send N; the rail and the chat list share it. */
export function CommentList({
  comments,
  agentLabel,
  wholeLabel,
  selected,
  onLocate,
  note,
}: CommentListProps) {
  const [ticks, setTicks] = useState<ReadonlySet<string>>(() => new Set());
  const [foldOpen, setFoldOpen] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  const {
    open,
    resolved,
    selected: chosen,
    canSend,
    sendLength,
  } = railState(comments.threads, ticks);
  const selectedResolved = resolved.some((thread) => thread.thread_id === selected);
  useEffect(() => {
    if (selectedResolved) setFoldOpen(true);
  }, [selectedResolved, selected]);
  useEffect(() => {
    if (!selected) return;
    list.current
      ?.closest(".rcp-comments, .rcp-comment-composer")
      ?.querySelector<HTMLElement>(`[data-thread-id="${CSS.escape(selected)}"]`)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [selected, foldOpen]);
  const card = (thread: CommentThread) => (
    <CommentThreadCard
      key={thread.thread_id}
      thread={thread}
      comments={comments}
      agentLabel={agentLabel}
      wholeLabel={wholeLabel}
      selected={thread.thread_id === selected}
      ticked={ticks.has(thread.thread_id)}
      onTick={(on) =>
        setTicks((current) => {
          const next = new Set(current);
          if (on) next.add(thread.thread_id);
          else next.delete(thread.thread_id);
          return next;
        })
      }
      onLocate={() => onLocate(thread)}
      note={note?.(thread)}
    />
  );
  return (
    <>
      <div className="rcp-comments-list" ref={list}>
        {open.map(card)}
      </div>
      {resolved.length > 0 && (
        <details
          className="rcp-comments-resolved"
          open={foldOpen}
          onToggle={(event) => setFoldOpen(event.currentTarget.open)}
        >
          <summary>Resolved · {resolved.length}</summary>
          <div>{resolved.map(card)}</div>
        </details>
      )}
      {comments.canSend && chosen.length > 0 && (
        <footer className="rcp-comments-footer">
          <button
            type="button"
            className="rcp-comment-primary"
            disabled={comments.busy || !canSend}
            title={sendLength > 32000 ? "The selected threads exceed 32,000 characters" : undefined}
            onClick={async () => {
              if (await comments.send(chosen.map((thread) => thread.thread_id)))
                setTicks(new Set());
            }}
          >
            Send {chosen.length} to agent
          </button>
        </footer>
      )}
      <div className="rcp-comments-notice" role="status">
        {comments.error}
      </div>
    </>
  );
}

/** The side rail: a header with whole-target Comment and Close, then the list.
 *  At phone width the shared stylesheet makes it a bottom sheet. */
export function CommentRail({
  className = "",
  onWhole,
  onClose,
  children,
  ...list
}: CommentListProps & {
  className?: string;
  onWhole?: () => void;
  onClose: () => void;
  children?: ReactNode;
}) {
  return (
    <aside className={`rcp-comments ${className}`} aria-label="Comments">
      <header className="rcp-comments-header">
        <strong>Comments</strong>
        <span className="rcp-comments-tools">
          {onWhole && list.comments.canPost && (
            <button type="button" onClick={onWhole} title={list.wholeLabel}>
              Comment
            </button>
          )}
          <button
            type="button"
            className="rcp-comments-close"
            aria-label="Close comments"
            onClick={onClose}
          >
            <X size={14} />
          </button>
        </span>
      </header>
      {children}
      <CommentList {...list} />
    </aside>
  );
}
