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
  /** The frame cancelled its selection (Escape, a new drag, an aborted one). */
  onCancel: () => void;
  /** The page reported an error, or that it cleared. */
  onPageError: (error: { message: string; count: number } | null) => void;
}

/** Talks to one inline viewer shell frame: reads its messages and keeps its comment mode.
 *
 * Returns the frame's `onLoad`, which repeats the comment mode a reload forgot. */
export function useInlineShell(
  frame: RefObject<HTMLIFrameElement | null>,
  commentMode: boolean,
  handlers: InlineShellHandlers,
): () => void {
  const commentModeRef = useRef(commentMode);
  commentModeRef.current = commentMode;
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;
  // Counts the frame's selections, so the clear of a replaced one leaves the new one alone.
  const selections = useRef(0);

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
        handlersRef.current.onCancel();
        return;
      }
      if (!commentModeRef.current) return;
      const rect = frame.current?.getBoundingClientRect();
      if (!rect) return;
      const box = value.selection.kind === "box" ? value.selection.rect : null;
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
        clear: () => {
          if (selection !== selections.current) return;
          frame.current?.contentWindow?.postMessage(
            { type: "rcp-inline-selection-clear", version: 1 },
            window.location.origin,
          );
        },
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
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the frame ref is stable
  }, [commentMode]);
  return () => postCommentMode(commentModeRef.current);
}
