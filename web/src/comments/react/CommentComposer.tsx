import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import type { Point } from "../box.ts";
import { anchorQuote } from "../model.ts";
import type { CommentAnchor, CommentTarget, CommentThread } from "../types.ts";
import { CommentEditor } from "./CommentEditor";
import { clearCommentHighlight, showCommentHighlight } from "./commentHighlight.ts";
import type { CommentsController } from "./useCommentActions";
import "../comments.css";

/** A new thread waiting for its first post: where it was drawn and on what. */
export interface CommentOffer {
  target: CommentTarget;
  anchor: CommentAnchor;
  drawnVersion: string | null;
  /** Where the popover opens, in window coordinates. */
  point: PopoverAnchor | null;
  /** The selected text, kept highlighted while the popover is open. */
  range?: Range;
  /** Removes the drawn mark, when the popover closes. */
  clear?: () => void;
  /** What the field starts with. */
  text?: string;
}

/** A popover's top-left, below its anchor. */
export interface PopoverAnchor extends Point {
  /** The anchor's top edge: without room below, the popover opens above it. */
  top?: number;
}

const MARGIN = 8;

/** Keeps a fixed popover beside its point and inside the window. */
function usePopoverPlacement(point: PopoverAnchor | null) {
  const ref = useRef<HTMLElement | null>(null);
  const [place, setPlace] = useState<{ left: number; top: number } | null>(null);
  useLayoutEffect(() => {
    const element = ref.current;
    if (!element) return;
    const { width, height } = element.getBoundingClientRect();
    const x = point?.x ?? window.innerWidth - width - MARGIN;
    const below = point ? point.y + MARGIN : 80;
    const above = point ? (point.top ?? point.y) - height - MARGIN : below;
    const y = below + height > window.innerHeight - MARGIN && above >= MARGIN ? above : below;
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
  closeOutside = false,
  onClose,
  children,
}: {
  point: PopoverAnchor | null;
  label: string;
  className?: string;
  /** A press outside it closes it too, except on the selection's Comment offer. */
  closeOutside?: boolean;
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
    const press = (event: PointerEvent) => {
      const target = event.target as Element | null;
      if (ref.current?.contains(target) || target?.closest?.(".chat-selection-comment")) return;
      close.current();
    };
    window.addEventListener("keydown", escape);
    if (closeOutside) document.addEventListener("pointerdown", press);
    return () => {
      window.removeEventListener("keydown", escape);
      document.removeEventListener("pointerdown", press);
    };
  }, [closeOutside, ref]);
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

/** The popover at a selection: its quote and a Comment field with `@`; saving docks the thread. */
export function CommentComposer({
  offer,
  comments,
  onClose,
  onCreated,
}: {
  offer: CommentOffer;
  comments: CommentsController;
  onClose: () => void;
  onCreated?: (thread: CommentThread) => void;
}) {
  const { clearError } = comments;
  useEffect(() => clearError(), [offer, clearError]);
  useEffect(() => {
    const owner = {};
    if (offer.range) showCommentHighlight(owner, [], [offer.range]);
    return () => clearCommentHighlight(owner);
  }, [offer.range]);
  return (
    <CommentPopover point={offer.point} label="Comment" closeOutside onClose={onClose}>
      <blockquote>{anchorQuote(offer.anchor)}</blockquote>
      <CommentEditor
        label="Comment"
        members={comments.members}
        busy={comments.busy}
        initialText={offer.text}
        autoFocus
        secondary={
          <button type="button" onClick={onClose}>
            Cancel
          </button>
        }
        onSubmit={async (text) => {
          const thread = await comments.create(
            offer.target,
            offer.anchor,
            text,
            offer.drawnVersion,
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
    </CommentPopover>
  );
}
