import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Point } from "../box.ts";
import { anchorQuote } from "../model.ts";
import type { CommentAnchor, CommentTarget, CommentThread } from "../types.ts";
import { CommentEditor } from "./CommentEditor";
import type { CommentsController } from "./useComments";
import "../comments.css";

/** A new thread waiting for its first post: where it was drawn and on what. */
export interface CommentOffer {
  target: CommentTarget;
  anchor: CommentAnchor;
  drawnVersion: string | null;
  /** Where the popover opens, in window coordinates. */
  point: Point | null;
  /** Removes the drawn mark, when the popover closes. */
  clear?: () => void;
}

const MARGIN = 8;

/** Keeps a fixed popover beside its point and inside the window. */
export function usePopoverPlacement(point: Point | null) {
  const ref = useRef<HTMLElement | null>(null);
  const [place, setPlace] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const { width, height } = element.getBoundingClientRect();
    const x = point?.x ?? window.innerWidth - width - MARGIN;
    const y = point ? point.y + MARGIN : 80;
    setPlace({
      left: Math.max(MARGIN, Math.min(x, window.innerWidth - width - MARGIN)),
      top: Math.max(MARGIN, Math.min(y, window.innerHeight - height - MARGIN)),
    });
  }, [point]);
  return { ref, style: place ?? { visibility: "hidden" as const } };
}

/** A fixed popover that closes on Escape; the composer and the chat thread share it. */
export function CommentPopover({
  point,
  label,
  className = "",
  onClose,
  children,
}: {
  point: Point | null;
  label: string;
  className?: string;
  onClose: () => void;
  children: ReactNode;
}) {
  const { ref, style } = usePopoverPlacement(point);
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") close.current();
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, []);
  return createPortal(
    <section
      ref={ref as React.RefObject<HTMLElement>}
      className={`rcp-comment-composer ${className}`}
      role="dialog"
      aria-label={label}
      style={style}
    >
      {children}
    </section>,
    document.body,
  );
}

/** The popover at a selection: its quote, a Comment field with `@`, and Send to agent. */
export function CommentComposer({
  offer,
  comments,
  agentLabel,
  canSend,
  onClose,
  onCreated,
}: {
  offer: CommentOffer;
  comments: CommentsController;
  agentLabel: string;
  /** This target's agent can take a send from this member. */
  canSend: boolean;
  onClose: () => void;
  onCreated?: (thread: CommentThread) => void;
}) {
  const { clearError } = comments;
  useEffect(() => clearError(), [offer, clearError]);
  return (
    <CommentPopover point={offer.point} label="Comment" onClose={onClose}>
      <blockquote>{anchorQuote(offer.anchor)}</blockquote>
      <CommentEditor
        label="Comment"
        members={comments.members}
        canSend={canSend && comments.canSend}
        agentLabel={agentLabel}
        busy={comments.busy}
        autoFocus
        onSubmit={async (text, send) => {
          const thread = await comments.create(
            offer.target,
            offer.anchor,
            text,
            offer.drawnVersion,
            send,
          );
          if (!thread) return false;
          onCreated?.(thread);
          onClose();
          return true;
        }}
      />
      {comments.error && (
        <div className="rcp-comments-notice" role="alert">
          {comments.error}
        </div>
      )}
      <div className="rcp-comment-actions">
        <button type="button" onClick={onClose}>
          Cancel
        </button>
      </div>
    </CommentPopover>
  );
}
