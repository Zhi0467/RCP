import { useEffect, useRef, useState, type ReactNode } from "react";
import { MessageSquarePlus, PanelRight, X } from "lucide-react";
import { railState } from "../model.ts";
import type { CommentThread } from "../types.ts";
import { CommentThreadCard } from "./CommentThreadCard";
import type { CommentsController } from "./useCommentActions";
import "../comments.css";
import "./rail.css";

export interface CommentListProps {
  comments: CommentsController;
  wholeLabel: string;
  selected: string | null;
  onLocate: (thread: CommentThread) => void;
  /** Why a thread has no pin here, shown on hover; undefined when it has one. */
  note?: (thread: CommentThread) => string | undefined;
}

/** The open threads and the Resolved fold; the rail and the chat list share it. */
export function CommentList({ comments, wholeLabel, selected, onLocate, note }: CommentListProps) {
  const [foldOpen, setFoldOpen] = useState(false);
  const list = useRef<HTMLDivElement>(null);
  const { open, resolved } = railState(comments.threads);
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
      wholeLabel={wholeLabel}
      selected={thread.thread_id === selected}
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
      <div className="rcp-comments-notice" role="status">
        {comments.error}
      </div>
    </>
  );
}

/** The side rail: a header with the comment-mode toggle, whole-target Comment and
 *  Close, then the list.
 *  At phone width the shared stylesheet makes it a bottom sheet. */
export function CommentRail({
  className = "",
  onWhole,
  onClose,
  onFold,
  commentMode,
  children,
  footer,
  ...list
}: CommentListProps & {
  className?: string;
  onWhole?: () => void;
  /** Present where parts can be picked; off leaves the page its own clicks. */
  commentMode?: { on: boolean; toggle: () => void };
  onClose?: () => void;
  /** Folds the rail away; the host shows its own control to bring it back. */
  onFold?: () => void;
  children?: ReactNode;
  footer?: ReactNode;
}) {
  return (
    <aside className={`rcp-comments ${className}`} aria-label="Comments">
      <header className="rcp-comments-header">
        <strong>Comments</strong>
        <span className="rcp-comments-tools">
          {commentMode && (
            <button
              type="button"
              className="rcp-comments-mode"
              aria-pressed={commentMode.on}
              aria-label={commentMode.on ? "Done commenting" : "Comment on a part"}
              title={
                commentMode.on
                  ? "Stop commenting and use the artifact"
                  : "Select a part of this artifact to comment on it"
              }
              onClick={commentMode.toggle}
            >
              <MessageSquarePlus size={14} />
            </button>
          )}
          {onWhole && list.comments.canPost && (
            <button type="button" onClick={onWhole} title={list.wholeLabel}>
              Comment
            </button>
          )}
          {onFold && (
            <button
              type="button"
              className="rcp-comments-fold"
              aria-label="Hide comments"
              title="Hide comments"
              onClick={onFold}
            >
              <PanelRight size={14} />
            </button>
          )}
          {onClose && (
            <button
              type="button"
              className="rcp-comments-close"
              aria-label="Close comments"
              onClick={onClose}
            >
              <X size={14} />
            </button>
          )}
        </span>
      </header>
      {children}
      <CommentList {...list} />
      {footer && <footer className="rcp-comments-footer">{footer}</footer>}
    </aside>
  );
}
