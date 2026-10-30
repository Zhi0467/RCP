import { useEffect, useRef, type RefObject } from "react";
import { readInlineShellMessage, type InlineSelection } from "./inlineArtifacts";

/** A selection the inline shell reported while comment mode was on. */
export interface InlineShellSelection {
  selection: InlineSelection;
  description: string;
  /** Where the comment window anchors, in the window's coordinates. */
  anchor: { left: number; right: number; top: number };
  /** Clears the selection drawn in the frame. */
  clear: () => void;
}

interface InlineShellHandlers {
  onSelection: (event: InlineShellSelection) => void;
  /** A selection is gone: the frame cancelled it (Escape, a click, a new drag) or the
   * owner called `cancel`. `clear` is the one that selection was reported with. */
  onCancel: (clear: () => void) => void;
  /** The page reported an error, or that it cleared. */
  onPageError: (error: { message: string; count: number } | null) => void;
}

/** Talks to one inline viewer shell frame: reads its messages and keeps its comment mode.
 * Leaving comment mode reports the frame's selection gone.
 *
 * Returns the frame's `onLoad`, which repeats the comment mode a reload forgot. */
export function useInlineShell(
  frame: RefObject<HTMLIFrameElement | null>,
  commentMode: boolean,
  handlers: InlineShellHandlers,
): { onLoad: () => void } {
  const commentModeRef = useRef(commentMode);
  commentModeRef.current = commentMode;
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  // Counts the frame's selection messages, cancels included, so the clear of a
  // replaced or cancelled selection does nothing to what the frame shows now.
  const selections = useRef(0);
  // The clear of the selection the frame shows, which a cancel names.
  const shown = useRef<(() => void) | null>(null);
  const cancel = () => {
    const clear = shown.current;
    shown.current = null;
    if (clear) handlersRef.current.onCancel(clear);
  };

  useEffect(() => {
    const message = (event: MessageEvent) => {
      const value = readInlineShellMessage(
        event,
        frame.current?.contentWindow,
        window.location.origin,
      );
      if (!value) return;
      if (value.kind === "error" || value.kind === "error-clear") {
        handlersRef.current.onPageError(value.kind === "error" ? value : null);
        return;
      }
      const selection = ++selections.current;
      if (!value.selection) {
        cancel();
        return;
      }
      if (!commentModeRef.current) return;
      const rect = frame.current?.getBoundingClientRect();
      if (!rect) return;
      const box = value.selection.kind === "box" ? value.selection.rect : null;
      const clear = () => {
        if (selection !== selections.current) return;
        frame.current?.contentWindow?.postMessage(
          { type: "rcp-inline-selection-clear", version: 1 },
          window.location.origin,
        );
      };
      shown.current = clear;
      handlersRef.current.onSelection({
        selection: value.selection,
        description: value.description,
        anchor: box
          ? {
              left: rect.left + box.x * rect.width,
              right: rect.left + (box.x + box.width) * rect.width,
              top: rect.top + box.y * rect.height,
            }
          : { left: rect.left, right: rect.right, top: rect.top + Math.min(rect.height, 48) },
        clear,
      });
    };
    window.addEventListener("message", message);
    return () => window.removeEventListener("message", message);
  }, [frame]);

  const postCommentMode = (enabled: boolean) =>
    frame.current?.contentWindow?.postMessage(
      { type: "rcp-inline-comment-mode", version: 1, enabled },
      window.location.origin,
    );
  useEffect(() => {
    postCommentMode(commentMode);
    if (!commentMode) cancel();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the frame ref is stable
  }, [commentMode]);
  return { onLoad: () => postCommentMode(commentModeRef.current) };
}
